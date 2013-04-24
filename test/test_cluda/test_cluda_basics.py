import itertools
from warnings import catch_warnings, filterwarnings

import pytest

import reikna.cluda as cluda
import reikna.cluda.dtypes as dtypes
import reikna.cluda.functions as functions
from reikna.helpers import product

from helpers import *
from pytest_threadgen import parametrize_thread_tuple, create_thread_in_tuple


TEST_DTYPES = [
    numpy.int8, numpy.int16, numpy.int32, numpy.int64,
    numpy.uint8, numpy.uint16, numpy.uint32, numpy.uint64,
    numpy.float32, numpy.float64,
    numpy.complex64, numpy.complex128]


pytest_funcarg__thr_and_global_size = create_thread_in_tuple


def pair_thread_with_gs(metafunc, tc):
    global_sizes = [
        (100,), (2000,), (1153,),
        (10, 10), (150, 250), (137, 547),
        (7, 11, 13), (50, 100, 100), (53, 101, 101)]

    rem_ids = []
    vals = []

    for gs in global_sizes:

        # If the thread will not support these limits, skip
        thr = tc()
        mgs = thr.device_params.max_num_groups
        del thr
        if len(gs) > len(mgs) or (len(mgs) > 2 and len(gs) > 2 and mgs[2] < gs[2]):
            continue

        rem_ids.append(str(gs))
        vals.append((gs,))

    return [tc] * len(vals), vals, rem_ids


def pytest_generate_tests(metafunc):
    if 'thr_and_global_size' in metafunc.funcargnames:
        parametrize_thread_tuple(metafunc, 'thr_and_global_size', pair_thread_with_gs)


def simple_thread_test(thr):
    shape = (1000,)
    dtype = numpy.float32

    a = get_test_array(shape, dtype)
    a_dev = thr.to_device(a)
    a_back = thr.from_device(a_dev)

    assert diff_is_negligible(a, a_back)


def test_create_new_thread(cluda_api):
    thr = cluda_api.Thread.create()
    simple_thread_test(thr)


def test_transfers(thr):
    a = get_test_array(1024, numpy.float32)

    def to_device1(x):
        return thr.to_device(x)
    def to_device2(x):
        y = thr.empty_like(x)
        thr.to_device(x, dest=y)
        return y
    def from_device1(x):
        return x.get()
    def from_device2(x):
        return thr.from_device(x)
    def from_device3(x):
        y = numpy.empty(x.shape, x.dtype)
        thr.from_device(x, dest=y)
        return y
    def from_device4(x):
        y = thr.from_device(x, async=True)
        thr.synchronize()
        return y
    def from_device5(x):
        y = numpy.empty(x.shape, x.dtype)
        thr.from_device(x, dest=y, async=True)
        thr.synchronize()
        return y

    to_device = (to_device1, to_device2)
    from_device = (from_device1, from_device2, from_device3, from_device4, from_device5)

    for to_d, from_d in itertools.product(to_device, from_device):
        a_device = to_d(a)
        a_copy = thr.copy_array(a_device)
        a_back = from_d(a_copy)
        assert diff_is_negligible(a, a_back)


@pytest.mark.parametrize(
    "dtype", TEST_DTYPES,
    ids=[dtypes.normalize_type(dtype).name for dtype in TEST_DTYPES])
def test_dtype_support(thr, dtype):
    # Test passes if either thread correctly reports that it does not support given dtype,
    # or it successfully compiles kernel that operates with this dtype.

    N = 256

    if not thr.supports_dtype(dtype):
        pytest.skip()

    mul = functions.mul(dtype, dtype)
    div = functions.div(dtype, dtype)
    program = thr.compile(
    """
    KERNEL void test(
        GLOBAL_MEM ${ctype} *dest, GLOBAL_MEM ${ctype} *a, GLOBAL_MEM ${ctype} *b)
    {
      const int i = get_global_id(0);
      ${ctype} temp = ${mul}(a[i], b[i]);
      dest[i] = ${div}(temp, b[i]);
    }
    """, render_kwds=dict(ctype=dtypes.ctype(dtype), dtype=dtype, mul=mul, div=div))

    test = program.test

    # we need results to fit even in unsigned char
    a = get_test_array(N, dtype, high=8)
    b = get_test_array(N, dtype, no_zeros=True, high=8)

    a_dev = thr.to_device(a)
    b_dev = thr.to_device(b)
    dest_dev = thr.empty_like(a_dev)
    test(dest_dev, a_dev, b_dev, global_size=N)
    assert diff_is_negligible(thr.from_device(dest_dev), a)


@pytest.mark.parametrize('in_dtypes', ["ii", "ff", "cc", "cfi", "ifccfi"])
@pytest.mark.parametrize('out_dtype', ["auto", "i", "f", "c"])
def test_multiarg_mul(thr, out_dtype, in_dtypes):
    """
    Checks multi-argument mul() with a variety of data types.
    """

    N = 256
    test_dtype = lambda idx: dict(i=numpy.int32, f=numpy.float32, c=numpy.complex64)[idx]
    in_dtypes = map(test_dtype, in_dtypes)
    out_dtype = dtypes.result_type(*in_dtypes) if out_dtype == 'auto' else test_dtype(out_dtype)
    if dtypes.is_double(out_dtype):
        # numpy thinks that int32 * float32 == float64,
        # but we still need to run this test on older videocards
        out_dtype = numpy.complex64 if dtypes.is_complex(out_dtype) else numpy.float32

    def reference_func(*args):
        res = product(args)
        if not dtypes.is_complex(out_dtype) and dtypes.is_complex(res.dtype):
            res = res.real
        return res.astype(out_dtype)

    src = """
    <%
        argnames = ["a" + str(i + 1) for i in xrange(len(in_dtypes))]
        in_ctypes = map(dtypes.ctype, in_dtypes)
        out_ctype = dtypes.ctype(out_dtype)
    %>
    KERNEL void test(
        GLOBAL_MEM ${out_ctype} *dest
        %for arg, ctype in zip(argnames, in_ctypes):
        , GLOBAL_MEM ${ctype} *${arg}
        %endfor
        )
    {
        const int i = get_global_id(0);
        %for arg, ctype in zip(argnames, in_ctypes):
        ${ctype} ${arg}_load = ${arg}[i];
        %endfor

        dest[i] = ${mul}(${", ".join([arg + "_load" for arg in argnames])});
    }
    """

    # Temporarily catching imaginary part truncation warnings
    with catch_warnings():
        filterwarnings("ignore", "", numpy.ComplexWarning)
        mul = functions.mul(*in_dtypes, out_dtype=out_dtype)

    program = thr.compile(src,
        render_kwds=dict(in_dtypes=in_dtypes, out_dtype=out_dtype, mul=mul))

    test = program.test

    # we need results to fit even in unsigned char
    arrays = [get_test_array(N, dt, no_zeros=True) for dt in in_dtypes]
    arrays_dev = map(thr.to_device, arrays)
    dest_dev = thr.array(N, out_dtype)

    test(dest_dev, *arrays_dev, global_size=N)
    assert diff_is_negligible(thr.from_device(dest_dev), reference_func(*arrays))


def test_find_local_size(thr_and_global_size):
    thr, global_size = thr_and_global_size

    """
    Check that if None is passed as local_size, kernel can find some local_size to run with
    (not necessarily optimal).
    """

    program = thr.compile(
    """
    KERNEL void test(GLOBAL_MEM int *dest)
    {
      const int i = get_global_id(0) +
        get_global_id(1) * get_global_size(0) +
        get_global_id(2) * get_global_size(1) * get_global_size(0);
      dest[i] = i;
    }
    """)
    test = program.test
    dest_dev = thr.array(global_size, numpy.int32)
    test(dest_dev, global_size=global_size)

    assert diff_is_negligible(dest_dev.get().ravel(),
        numpy.arange(product(global_size)).astype(numpy.int32))
