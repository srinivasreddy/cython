from Errors import CompileError, error
import ExprNodes
from ExprNodes import IntNode, NoneNode, IntBinopNode, NameNode, AttributeNode
from Visitor import CythonTransform
import Options
from Code import UtilityCode
from UtilityCode import CythonUtilityCode
from PyrexTypes import py_object_type, cython_memoryview_ptr_type
import Buffer
import PyrexTypes

START_ERR = "there must be nothing or the value 0 (zero) in the start slot."
STOP_ERR = "Axis specification only allowed in the 'stop' slot."
STEP_ERR = "Only the value 1 (one) or valid axis specification allowed in the step slot."
ONE_ERR = "The value 1 (one) may appear in the first or last axis specification only."
BOTH_CF_ERR = "Cannot specify an array that is both C and Fortran contiguous."
INVALID_ERR = "Invalid axis specification."
EXPR_ERR = "no expressions allowed in axis spec, only names and literals."
CF_ERR = "Invalid axis specification for a C/Fortran contiguous array."
ERR_UNINITIALIZED = ("Cannot check if memoryview %s is initialized without the "
                     "GIL, consider using initializedcheck(False)")

def err_if_nogil_initialized_check(pos, env, name='variable'):
    if env.nogil and env.directives['initializedcheck']:
        error(pos, ERR_UNINITIALIZED % name)

def concat_flags(*flags):
    return "(%s)" % "|".join(flags)

format_flag = "PyBUF_FORMAT"

memview_c_contiguous = "(PyBUF_C_CONTIGUOUS | PyBUF_FORMAT | PyBUF_WRITABLE)"
memview_f_contiguous = "(PyBUF_F_CONTIGUOUS | PyBUF_FORMAT | PyBUF_WRITABLE)"
memview_any_contiguous = "(PyBUF_ANY_CONTIGUOUS | PyBUF_FORMAT | PyBUF_WRITABLE)"
memview_full_access = "PyBUF_FULL"
#memview_strided_access = "PyBUF_STRIDED"
memview_strided_access = "PyBUF_RECORDS"

MEMVIEW_DIRECT = '__Pyx_MEMVIEW_DIRECT'
MEMVIEW_PTR    = '__Pyx_MEMVIEW_PTR'
MEMVIEW_FULL   = '__Pyx_MEMVIEW_FULL'
MEMVIEW_CONTIG = '__Pyx_MEMVIEW_CONTIG'
MEMVIEW_STRIDED= '__Pyx_MEMVIEW_STRIDED'
MEMVIEW_FOLLOW = '__Pyx_MEMVIEW_FOLLOW'

_spec_to_const = {
        'direct' : MEMVIEW_DIRECT,
        'ptr'    : MEMVIEW_PTR,
        'full'   : MEMVIEW_FULL,
        'contig' : MEMVIEW_CONTIG,
        'strided': MEMVIEW_STRIDED,
        'follow' : MEMVIEW_FOLLOW,
        }

_spec_to_abbrev = {
    'direct'  : 'd',
    'ptr'     : 'p',
    'full'    : 'f',
    'contig'  : 'c',
    'strided' : 's',
    'follow'  : '_',
}

memview_name = u'memoryview'
memview_typeptr_cname = '__pyx_memoryview_type'
memview_objstruct_cname = '__pyx_memoryview_obj'
memviewslice_cname = u'__Pyx_memviewslice'

def put_init_entry(mv_cname, code):
    code.putln("%s.data = NULL;" % mv_cname)
    code.putln("%s.memview = NULL;" % mv_cname)

def mangle_dtype_name(dtype):
    # a dumb wrapper for now; move Buffer.mangle_dtype_name in here later?
    import Buffer
    return Buffer.mangle_dtype_name(dtype)

#def axes_to_str(axes):
#    return "".join([access[0].upper()+packing[0] for (access, packing) in axes])

def put_acquire_memoryviewslice(lhs_cname, lhs_type, lhs_pos, rhs, code,
                                incref_rhs=True, have_gil=False):
    assert rhs.type.is_memoryviewslice

    pretty_rhs = isinstance(rhs, NameNode) or rhs.result_in_temp()
    if pretty_rhs:
        rhstmp = rhs.result()
    else:
        rhstmp = code.funcstate.allocate_temp(lhs_type, manage_ref=False)
        code.putln("%s = %s;" % (rhstmp, rhs.result_as(lhs_type)))

    # Allow uninitialized assignment
    #code.putln(code.put_error_if_unbound(lhs_pos, rhs.entry))
    put_assign_to_memviewslice(lhs_cname, rhstmp, lhs_type, code, incref_rhs)

    if not pretty_rhs:
        code.funcstate.release_temp(rhstmp)

def put_assign_to_memviewslice(lhs_cname, rhs_cname, memviewslicetype, code,
                               incref_rhs=True):
    code.put_xdecref_memoryviewslice(lhs_cname)
    if incref_rhs:
        code.put_incref_memoryviewslice(rhs_cname)

    code.putln("%s.memview = %s.memview;" % (lhs_cname, rhs_cname))
    code.putln("%s.data = %s.data;" % (lhs_cname, rhs_cname))
    ndim = len(memviewslicetype.axes)
    for i in range(ndim):
        code.putln("%s.shape[%d] = %s.shape[%d];" % (lhs_cname, i, rhs_cname, i))
        code.putln("%s.strides[%d] = %s.strides[%d];" % (lhs_cname, i, rhs_cname, i))
        code.putln("%s.suboffsets[%d] = %s.suboffsets[%d];" % (lhs_cname, i, rhs_cname, i))

def get_buf_flags(specs):
    is_c_contig, is_f_contig = is_cf_contig(specs)

    if is_c_contig:
        return memview_c_contiguous
    elif is_f_contig:
        return memview_f_contiguous

    access, packing = zip(*specs)

    assert 'follow' not in packing

    if 'full' in access or 'ptr' in access:
        return memview_full_access
    else:
        return memview_strided_access


def use_cython_array(env):
    env.use_utility_code(cython_array_utility_code)

def src_conforms_to_dst(src, dst):
    '''
    returns True if src conforms to dst, False otherwise.

    If conformable, the types are the same, the ndims are equal, and each axis spec is conformable.

    Any packing/access spec is conformable to itself.

    'direct' and 'ptr' are conformable to 'full'.
    'contig' and 'follow' are conformable to 'strided'.
    Any other combo is not conformable.
    '''

    if src.dtype != dst.dtype:
        return False
    if len(src.axes) != len(dst.axes):
        return False

    for src_spec, dst_spec in zip(src.axes, dst.axes):
        src_access, src_packing = src_spec
        dst_access, dst_packing = dst_spec
        if src_access != dst_access and dst_access != 'full':
            return False
        if src_packing != dst_packing and dst_packing != 'strided':
            return False

    return True


class MemoryViewSliceBufferEntry(Buffer.BufferEntry):
    def __init__(self, entry):
        self.entry = entry
        self.type = entry.type
        self.cname = entry.cname
        self.buf_ptr = "%s.data" % self.cname

        dtype = self.entry.type.dtype
        dtype = PyrexTypes.CPtrType(dtype)

        self.buf_ptr_type = dtype

    def get_buf_suboffsetvars(self):
        return self._for_all_ndim("%s.suboffsets[%d]")

    def get_buf_stridevars(self):
        return self._for_all_ndim("%s.strides[%d]")

    def get_buf_shapevars(self):
        return self._for_all_ndim("%s.shape[%d]")

    def generate_buffer_lookup_code(self, code, index_cnames):
        bufp = self.buf_ptr
        type_decl = self.type.dtype.declaration_code("")

        for dim, (access, packing) in enumerate(self.type.axes):
            shape = "%s.shape[%d]" % (self.cname, dim)
            stride = "%s.strides[%d]" % (self.cname, dim)
            suboffset = "%s.suboffsets[%d]" % (self.cname, dim)
            index = index_cnames[dim]

            flag = get_memoryview_flag(access, packing)

            if flag in ("generic", "generic_contiguous"):
                # Note: we cannot do cast tricks to avoid stride multiplication
                #       for generic_contiguous, as we may have to do (dtype *)
                #       or (dtype **) arithmetic, we won't know which unless
                #       we check suboffsets
                code.globalstate.use_utility_code(memviewslice_index_helpers)
                bufp = ('__pyx_memviewslice_index_full(%s, %s, %s, %s)' %
                                            (bufp, index, stride, suboffset))

            elif flag == "indirect":
                bufp = "(%s + %s * %s)" % (bufp, index, stride)
                bufp = ("(*((char **) %s) + %s)" % (bufp, suboffset))

            elif flag == "indirect_contiguous":
                # Note: we do char ** arithmetic
                bufp = "(*((char **) %s + %s) + %s)" % (bufp, index, suboffset)

            elif flag == "strided":
                bufp = "(%s + %s * %s)" % (bufp, index, stride)

            else:
                assert flag == 'contiguous', flag
                bufp = '((char *) (((%s *) %s) + %s))' % (type_decl, bufp, index)

            bufp = '( /* dim=%d */ %s )' % (dim, bufp)

        return "((%s *) %s)" % (type_decl, bufp)

def get_memoryview_flag(access, packing):
    if access == 'full' and packing in ('strided', 'follow'):
        return 'generic'
    elif access == 'full' and packing == 'contig':
        return 'generic_contiguous'
    elif access == 'ptr' and packing in ('strided', 'follow'):
        return 'indirect'
    elif access == 'ptr' and packing == 'contig':
        return 'indirect_contiguous'
    elif access == 'direct' and packing in ('strided', 'follow'):
        return 'strided'
    else:
        assert (access, packing) == ('direct', 'contig'), (access, packing)
        return 'contiguous'

def get_copy_func_name(to_memview):
    base = "__Pyx_BufferNew_%s_From_%s"
    if to_memview.is_c_contig:
        return base % ('C', to_memview.specialization_suffix())
    else:
        return base % ('F', to_memview.specialization_suffix())

def get_copy_contents_name(from_mvs, to_mvs):
    assert from_mvs.dtype == to_mvs.dtype
    return '__Pyx_BufferCopyContents_%s_to_%s' % (from_mvs.specialization_suffix(),
                                                  to_mvs.specialization_suffix())


class IsContigFuncUtilCode(object):
    
    requires = None

    def __init__(self, c_or_f):
        self.c_or_f = c_or_f

        self.is_contig_func_name = get_is_contig_func_name(self.c_or_f)

    def __eq__(self, other):
        if not isinstance(other, IsContigFuncUtilCode):
            return False
        return self.is_contig_func_name == other.is_contig_func_name

    def __hash__(self):
        return hash(self.is_contig_func_name)

    def get_tree(self): pass

    def put_code(self, output):
        code = output['utility_code_def']
        proto = output['utility_code_proto']

        func_decl, func_impl = get_is_contiguous_func(self.c_or_f)

        proto.put(func_decl)
        code.put(func_impl)

def get_is_contig_func_name(c_or_f):
    return "__Pyx_Buffer_is_%s_contiguous" % c_or_f

def get_is_contiguous_func(c_or_f):

    func_name = get_is_contig_func_name(c_or_f)
    decl = "static int %s(const __Pyx_memviewslice); /* proto */\n" % func_name

    impl = """
static int %s(const __Pyx_memviewslice mvs) {
    /* returns 1 if mvs is the right contiguity, 0 otherwise */

    int i, ndim = mvs.memview->view.ndim;
    Py_ssize_t itemsize = mvs.memview->view.itemsize;
    long size = 0;
""" % func_name

    if c_or_f == 'fortran':
        for_loop = "for(i=0; i<ndim; i++)"
    elif c_or_f == 'c':
        for_loop = "for(i=ndim-1; i>-1; i--)"
    else:
        assert False

    impl += """
    size = 1;
    %(for_loop)s {

#ifdef DEBUG
        printf("mvs.suboffsets[i] %%d\\n", mvs.suboffsets[i]);
        printf("mvs.strides[i] %%d\\n", mvs.strides[i]);
        printf("mvs.shape[i] %%d\\n", mvs.shape[i]);
        printf("size %%d\\n", size);
        printf("ndim %%d\\n", ndim);
#endif
#undef DEBUG

        if(mvs.suboffsets[i] >= 0) {
            return 0;
        }
        if(size * itemsize != mvs.strides[i]) {
            return 0;
        }
        size *= mvs.shape[i];
    }
    return 1;

}""" % {'for_loop' : for_loop}

    return decl, impl

copy_to_template = '''
static int %(copy_to_name)s(const __Pyx_memviewslice from_mvs, __Pyx_memviewslice to_mvs) {

    /* ensure from_mvs & to_mvs have the same shape & dtype */

}
'''

class CopyContentsFuncUtilCode(object):
    
    requires = None

    def __init__(self, from_memview, to_memview):
        self.from_memview = from_memview
        self.to_memview = to_memview
        self.copy_contents_name = get_copy_contents_name(from_memview, to_memview)

    def __eq__(self, other):
        if not isinstance(other, CopyContentsFuncUtilCode):
            return False
        return other.copy_contents_name == self.copy_contents_name

    def __hash__(self):
        return hash(self.copy_contents_name)

    def get_tree(self): pass

    def put_code(self, output):
        code = output['utility_code_def']
        proto = output['utility_code_proto']

        func_decl, func_impl = \
                get_copy_contents_func(self.from_memview, self.to_memview, self.copy_contents_name)

        proto.put(func_decl)
        code.put(func_impl)

class CopyFuncUtilCode(object):

    requires = None

    def __init__(self, from_memview, to_memview):
        if from_memview.dtype != to_memview.dtype:
            raise ValueError("dtypes must be the same!")
        if len(from_memview.axes) != len(to_memview.axes):
            raise ValueError("number of dimensions must be same")
        if not (to_memview.is_c_contig or to_memview.is_f_contig):
            raise ValueError("to_memview must be c or f contiguous.")
        for (access, packing) in from_memview.axes:
            if access != 'direct':
                raise NotImplementedError("cannot handle 'full' or 'ptr' access at this time.")

        self.from_memview = from_memview
        self.to_memview = to_memview
        self.copy_func_name = get_copy_func_name(to_memview)

        self.requires = [CopyContentsFuncUtilCode(from_memview, to_memview)]

    def __eq__(self, other):
        if not isinstance(other, CopyFuncUtilCode):
            return False
        return other.copy_func_name == self.copy_func_name

    def __hash__(self):
        return hash(self.copy_func_name)

    def get_tree(self): pass

    def put_code(self, output):
        code = output['utility_code_def']
        proto = output['utility_code_proto']

        proto.put(Buffer.dedent("""\
                static __Pyx_memviewslice %s(const __Pyx_memviewslice from_mvs); /* proto */
        """ % self.copy_func_name))

        copy_contents_name = get_copy_contents_name(self.from_memview, self.to_memview)

        if self.to_memview.is_c_contig:
            mode = 'c'
            contig_flag = memview_c_contiguous
        elif self.to_memview.is_f_contig:
            mode = 'fortran'
            contig_flag = memview_f_contiguous

        context = dict(
            copy_name=self.copy_func_name,
            mode=mode,
            sizeof_dtype="sizeof(%s)" % self.from_memview.dtype.declaration_code(''),
            contig_flag=contig_flag,
            copy_contents_name=copy_contents_name
        )

        _, copy_code = UtilityCode.load_as_string("MemviewSliceCopyTemplate",
                                                  from_file="MemoryView_C.c",
                                                  context=context)
        code.put(copy_code)


def get_copy_contents_func(from_mvs, to_mvs, cfunc_name):
    assert from_mvs.dtype == to_mvs.dtype
    assert len(from_mvs.axes) == len(to_mvs.axes)

    ndim = len(from_mvs.axes)

    # XXX: we only support direct access for now.
    for (access, packing) in from_mvs.axes:
        if access != 'direct':
            raise NotImplementedError("currently only direct access is supported.")

    code_decl = ("static int %(cfunc_name)s(const __Pyx_memviewslice *from_mvs,"
                "__Pyx_memviewslice *to_mvs); /* proto */" % {'cfunc_name' : cfunc_name})

    code_impl = '''

static int %(cfunc_name)s(const __Pyx_memviewslice *from_mvs, __Pyx_memviewslice *to_mvs) {

    char *to_buf = (char *)to_mvs->data;
    char *from_buf = (char *)from_mvs->data;
    struct __pyx_memoryview_obj *temp_memview = 0;
    char *temp_data = 0;

    int ndim_idx = 0;

    for(ndim_idx=0; ndim_idx<%(ndim)d; ndim_idx++) {
        if(from_mvs->shape[ndim_idx] != to_mvs->shape[ndim_idx]) {
            PyErr_Format(PyExc_ValueError,
                "memoryview shapes not the same in dimension %%d", ndim_idx);
            return -1;
        }
    }

''' % {'cfunc_name' : cfunc_name, 'ndim' : ndim}

    # raise NotImplementedError("put in shape checking code here!!!")

    INDENT = "    "
    dtype_decl = from_mvs.dtype.declaration_code("")
    last_idx = ndim-1

    if to_mvs.is_c_contig or to_mvs.is_f_contig:
        if to_mvs.is_c_contig:
            start, stop, step = 0, ndim, 1
        elif to_mvs.is_f_contig:
            start, stop, step = ndim-1, -1, -1


        for i, idx in enumerate(range(start, stop, step)):
            # the crazy indexing is to account for the fortran indexing.
            # 'i' always goes up from zero to ndim-1.
            # 'idx' is the same as 'i' for c_contig, and goes from ndim-1 to 0 for f_contig.
            # this makes the loop code below identical in both cases.
            code_impl += INDENT+"Py_ssize_t i%d = 0, idx%d = 0;\n" % (i,i)
            code_impl += INDENT+"Py_ssize_t stride%(i)d = from_mvs->strides[%(idx)d];\n" % {'i':i, 'idx':idx}
            code_impl += INDENT+"Py_ssize_t shape%(i)d = from_mvs->shape[%(idx)d];\n" % {'i':i, 'idx':idx}

        code_impl += "\n"

        # put down the nested for-loop.
        for k in range(ndim):

            code_impl += INDENT*(k+1) + "for(i%(k)d=0; i%(k)d<shape%(k)d; i%(k)d++) {\n" % {'k' : k}
            if k >= 1:
                code_impl += INDENT*(k+2) + "idx%(k)d = i%(k)d * stride%(k)d + idx%(km1)d;\n" % {'k' : k, 'km1' : k-1}
            else:
                code_impl += INDENT*(k+2) + "idx%(k)d = i%(k)d * stride%(k)d;\n" % {'k' : k}

        # the inner part of the loop.
        code_impl += INDENT*(ndim+1)+"memcpy(to_buf, from_buf+idx%(last_idx)d, sizeof(%(dtype_decl)s));\n" % locals()
        code_impl += INDENT*(ndim+1)+"to_buf += sizeof(%(dtype_decl)s);\n" % locals()


    else:

        code_impl += INDENT+"/* 'f' prefix is for the 'from' memview, 't' prefix is for the 'to' memview */\n"
        for i in range(ndim):
            code_impl += INDENT+"char *fi%d = 0, *ti%d = 0, *end%d = 0;\n" % (i,i,i)
            code_impl += INDENT+"Py_ssize_t fstride%(i)d = from_mvs->strides[%(i)d];\n" % {'i':i}
            code_impl += INDENT+"Py_ssize_t fshape%(i)d = from_mvs->shape[%(i)d];\n" % {'i':i}
            code_impl += INDENT+"Py_ssize_t tstride%(i)d = to_mvs->strides[%(i)d];\n" % {'i':i}
            # code_impl += INDENT+"Py_ssize_t tshape%(i)d = to_mvs->shape[%(i)d];\n" % {'i':i}

        code_impl += INDENT+"end0 = fshape0 * fstride0 + from_mvs->data;\n"
        code_impl += INDENT+"for(fi0=from_buf, ti0=to_buf; fi0 < end0; fi0 += fstride0, ti0 += tstride0) {\n"
        for i in range(1, ndim):
            code_impl += INDENT*(i+1)+"end%(i)d = fshape%(i)d * fstride%(i)d + fi%(im1)d;\n" % {'i' : i, 'im1' : i-1}
            code_impl += INDENT*(i+1)+"for(fi%(i)d=fi%(im1)d, ti%(i)d=ti%(im1)d; fi%(i)d < end%(i)d; fi%(i)d += fstride%(i)d, ti%(i)d += tstride%(i)d) {\n" % {'i':i, 'im1':i-1}

        code_impl += INDENT*(ndim+1)+"*(%(dtype_decl)s*)(ti%(last_idx)d) = *(%(dtype_decl)s*)(fi%(last_idx)d);\n" % locals()

    # for-loop closing braces
    for k in range(ndim-1, -1, -1):
        code_impl += INDENT*(k+1)+"}\n"

    # init to_mvs->data and to_mvs shape/strides/suboffsets arrays.
    code_impl += INDENT+"temp_memview = to_mvs->memview;\n"
    code_impl += INDENT+"temp_data = to_mvs->data;\n"
    code_impl += INDENT+"to_mvs->memview = 0; to_mvs->data = 0;\n"
    code_impl += INDENT+"if(unlikely(-1 == __Pyx_init_memviewslice(temp_memview, %d, to_mvs))) {\n" % (ndim,)
    code_impl += INDENT*2+"return -1;\n"
    code_impl +=   INDENT+"}\n"

    code_impl += INDENT + "return 0;\n"

    code_impl += '}\n'

    return code_decl, code_impl

def get_axes_specs(env, axes):
    '''
    get_axes_specs(env, axes) -> list of (access, packing) specs for each axis.

    access is one of 'full', 'ptr' or 'direct'
    packing is one of 'contig', 'strided' or 'follow'
    '''

    cythonscope = env.global_scope().context.cython_scope
    viewscope = cythonscope.viewscope

    access_specs = tuple([viewscope.lookup(name)
                    for name in ('full', 'direct', 'ptr')])
    packing_specs = tuple([viewscope.lookup(name)
                    for name in ('contig', 'strided', 'follow')])

    is_f_contig, is_c_contig = False, False
    default_access, default_packing = 'direct', 'strided'
    cf_access, cf_packing = default_access, 'follow'

    # set the is_{c,f}_contig flag.
    for idx, axis in ((0,axes[0]), (-1,axes[-1])):
        if isinstance(axis.step, IntNode):
            if axis.step.compile_time_value(env) != 1:
                raise CompileError(axis.step.pos, STEP_ERR)
            if len(axes) > 1 and (is_c_contig or is_f_contig):
                raise CompileError(axis.step.pos, BOTH_CF_ERR)
            if not idx:
                is_f_contig = True
            else:
                is_c_contig = True
            if len(axes) == 1:
                break

    assert not (is_c_contig and is_f_contig)

    axes_specs = []
    # analyse all axes.
    for idx, axis in enumerate(axes):

        # start slot can be either a literal '0' or None.
        if isinstance(axis.start, IntNode):
            if axis.start.compile_time_value(env):
                raise CompileError(axis.start.pos,  START_ERR)
        elif not isinstance(axis.start, NoneNode):
            raise CompileError(axis.start.pos,  START_ERR)

        # stop slot must be None.
        if not isinstance(axis.stop, NoneNode):
            raise CompileError(axis.stop.pos, STOP_ERR)

        # step slot can be None, the value 1, 
        # a single axis spec, or an IntBinopNode.
        if isinstance(axis.step, NoneNode):
            if is_c_contig or is_f_contig:
                axes_specs.append((cf_access, cf_packing))
            else:
                axes_specs.append((default_access, default_packing))

        elif isinstance(axis.step, IntNode):
            if idx not in (0, len(axes)-1):
                raise CompileError(axis.step.pos, ONE_ERR)
            # the packing for the ::1 axis is contiguous, 
            # all others are cf_packing.
            axes_specs.append((cf_access, 'contig'))

        elif isinstance(axis.step, (NameNode, AttributeNode)):
            if is_c_contig or is_f_contig:
                raise CompileError(axis.step.pos, CF_ERR)

            entry = _get_resolved_spec(env, axis.step)
            if entry.name in view_constant_to_access_packing:
                axes_specs.append(view_constant_to_access_packing[entry.name])
            else:
                raise CompilerError(axis.step.pos, INVALID_ERR)

        else:
            raise CompileError(axis.step.pos, INVALID_ERR)

    validate_axes_specs([axis.start.pos for axis in axes], axes_specs)

    return axes_specs

def all(it):
    for item in it:
        if not item:
            return False
    return True

def is_cf_contig(specs):
    is_c_contig = is_f_contig = False

    if (len(specs) == 1 and specs == [('direct', 'contig')]):
        is_c_contig = True

    elif (specs[-1] == ('direct','contig') and
          all([axis == ('direct','follow') for axis in specs[:-1]])):
        # c_contiguous: 'follow', 'follow', ..., 'follow', 'contig'
        is_c_contig = True

    elif (len(specs) > 1 and 
        specs[0] == ('direct','contig') and
        all([axis == ('direct','follow') for axis in specs[1:]])):
        # f_contiguous: 'contig', 'follow', 'follow', ..., 'follow'
        is_f_contig = True

    return is_c_contig, is_f_contig

def get_mode(specs):
    is_c_contig, is_f_contig = is_cf_contig(specs)

    if is_c_contig:
        return 'c'
    elif is_f_contig:
        return 'fortran'

    for access, packing in specs:
        if access in ('ptr', 'full'):
            return 'full'

    return 'strided'

view_constant_to_access_packing = {
    'generic':              ('full',   'strided'),
    'strided':              ('direct', 'strided'),
    'indirect':             ('ptr',    'strided'),
    'generic_contiguous':   ('full',   'contig'),
    'contiguous':           ('direct', 'contig'),
    'indirect_contiguous':  ('ptr',    'contig'),
}

def get_access_packing(view_scope_constant):
    if view_scope_constant.name == 'generic':
        return 'full',

def validate_axes_specs(positions, specs):

    packing_specs = ('contig', 'strided', 'follow')
    access_specs = ('direct', 'ptr', 'full')

    is_c_contig, is_f_contig = is_cf_contig(specs)
    
    has_contig = has_follow = has_strided = has_generic_contig = False

    for pos, (access, packing) in zip(positions, specs):

        if not (access in access_specs and
                packing in packing_specs):
            raise CompileError(pos, "Invalid axes specification.")

        if packing == 'strided':
            has_strided = True
        elif packing == 'contig':
            if has_contig:
                if access == 'ptr':
                    raise CompileError(pos, "Indirect contiguous dimensions must precede direct contiguous")
                elif has_generic_contig or access == 'full':
                    raise CompileError(pos, "Generic contiguous cannot be combined with direct contiguous")
                else:
                    raise CompileError(pos, "Only one direct contiguous axis may be specified.")
            # Note: We do NOT allow access == 'full' to act as
            #       "additionally potentially contiguous"
            has_contig = access != 'ptr'
            has_generic_contig = has_generic_contig or access == 'full'
        elif packing == 'follow':
            if has_strided:
                raise CompileError(pos, "A memoryview cannot have both follow and strided axis specifiers.")
            if not (is_c_contig or is_f_contig):
                raise CompileError(pos, "Invalid use of the follow specifier.")

def _get_resolved_spec(env, spec):
    # spec must be a NameNode or an AttributeNode
    if isinstance(spec, NameNode):
        return _resolve_NameNode(env, spec)
    elif isinstance(spec, AttributeNode):
        return _resolve_AttributeNode(env, spec)
    else:
        raise CompileError(spec.pos, INVALID_ERR)

def _resolve_NameNode(env, node):
    try:
        resolved_name = env.lookup(node.name).name
    except AttributeError:
        raise CompileError(node.pos, INVALID_ERR)
    viewscope = env.global_scope().context.cython_scope.viewscope
    return viewscope.lookup(resolved_name)

def _resolve_AttributeNode(env, node):
    path = []
    while isinstance(node, AttributeNode):
        path.insert(0, node.attribute)
        node = node.obj
    if isinstance(node, NameNode):
        path.insert(0, node.name)
    else:
        raise CompileError(node.pos, EXPR_ERR)
    modnames = path[:-1]
    # must be at least 1 module name, o/w not an AttributeNode.
    assert modnames

    scope = env
    for modname in modnames:
        mod = scope.lookup(modname)
        if not mod:
            raise CompileError(
                    node.pos, "undeclared name not builtin: %s" % modname)
        scope = mod.as_module

    return scope.lookup(path[-1])

def load_memview_cy_utility(util_code_name, context=None, **kwargs):
    return CythonUtilityCode.load(util_code_name, "MemoryView.pyx",
                                  context=context, **kwargs)

def load_memview_c_utility(util_code_name, context=None, **kwargs):
    return UtilityCode.load(util_code_name, "MemoryView_C.c",
                            context=context, **kwargs)

context = {
    'memview_struct_name': memview_objstruct_cname,
    'max_dims': Options.buffer_max_dims,
    'memviewslice_name': memviewslice_cname,
}
memviewslice_declare_code = load_memview_c_utility(
        "MemviewSliceStruct",
        proto_block='utility_code_proto_before_types',
        context=context)

memviewslice_init_code = load_memview_c_utility(
    "MemviewSliceInit",
    context=dict(context, BUF_MAX_NDIMS=Options.buffer_max_dims),
    requires=[memviewslice_declare_code,
              Buffer.acquire_utility_code],
)

memviewslice_index_helpers = load_memview_c_utility("MemviewSliceIndex")

typeinfo_to_format_code = load_memview_cy_utility(
        "BufferFormatFromTypeInfo", requires=[Buffer._typeinfo_to_format_code])

view_utility_code = load_memview_cy_utility(
        "View.MemoryView",
        context=context,
        requires=[Buffer.GetAndReleaseBufferUtilityCode(),
                  Buffer.buffer_struct_declare_code,
                  Buffer.empty_bufstruct_utility,
                  memviewslice_init_code],
)

cython_array_utility_code = load_memview_cy_utility(
        "CythonArray",
        context=context,
        requires=[view_utility_code])

memview_fromslice_utility_code = load_memview_cy_utility(
        "MemviewFromSlice",
        context=context,
        requires=[view_utility_code],
)