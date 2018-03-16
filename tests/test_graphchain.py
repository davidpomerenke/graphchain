"""
Test module for the graphchain and funcutils modules.
Based on the 'pytest' test framework.
"""
import os
import shutil
import pickle
import dask
import pytest
from dask.optimization import get_dependencies
from context import graphchain
from graphchain import gcoptimize
from funcutils import isiterable

@pytest.fixture(scope="function")
def dask_dag_generation():
    """
    A function that generates a dask compatible
    graph of the form, which will be used as a
    basis for the functional testing of the
    graphchain module:

		     O top(..)
                 ____|____
		/	  \
               d1          O baz(..)
		  _________|________
                 /                  \
                O boo(...)           O goo(...)
         _______|_______         ____|____
	/       |       \       /    |    \
       O        O        O     O     |     O
     foo(.) bar(.)    baz(.)  foo(.) v6  bar(.)
      |         |        |     |           |
      |         |        |     |           |
      v1       v2       v3    v4          v5
    """

    # Functions
    def foo(argument):
        return argument

    def bar(argument):
        return argument + 2

    def baz(*args):
        return sum(args)

    def boo(*args):
        return len(args)+sum(args)

    def goo(*args):
        return sum(args) + 1

    def top(argument, argument2):
        return argument - argument2

    # Graph (for the function definitions above)
    dsk = {"v1":1, "v2":2, "v3":3, "v4":0,
           "v5":-1, "v6":-2, "d1":-3,
           "foo1": (foo, "v1"),
           "foo2": (foo, "v4"),
           "bar1": (bar, "v2"),
           "bar2": (bar, "v5"),
           "baz1": (baz, "v3"),
           "baz2": (baz, "boo1", "goo1"),
           "boo1": (boo, "foo1", "bar1", "baz1"),
           "goo1": (goo, "foo2", "bar2", "v6"),
           "top1": (top, "d1", "baz2")}
    return dsk


def test_dag(dask_dag_generation):
    """
    Function that tests that the dask DAG can be
    traversed correctly and that the actual result
    for the 'top1' key is correct.
    """
    dsk = dask_dag_generation
    result = dask.get(dsk, ["top1"])
    assert result == (-14,)


@pytest.fixture(scope="module")
def temporary_directory():
    """
    Function that creates the directory used for the
    graphchain tests. After the tests finish, it will
    be removed.
    """
    directory = os.path.abspath('__pytest_graphchain_cache__')
    if os.path.isdir(directory):
        shutil.rmtree(directory, ignore_errors=True)
    os.mkdir(directory, mode=0o777)
    yield directory
    shutil.rmtree(directory, ignore_errors=True)
    print("Cleanup of {} complete.".format(directory))
    return  directory


def test_first_run(temporary_directory, dask_dag_generation):
    """
    Function that tests a first run of the graphchain
    optimization function 'gcoptimize'. It checks the
    final result, that that all function calls are
    wrapped - for execution and output storing, that the
    hashchain is created, that hashed outputs
    (the <hash>.bin files) are generated and that the
    name of each file is a key in the hashchain.
    """
    tmpdir = temporary_directory
    dsk = dask_dag_generation
    newdsk = gcoptimize(dsk, keys=["top1"], cachedir=tmpdir, verbose=True)

    # Check the final result
    result = dask.get(newdsk, ["top1"])
    assert result == (-14,)

    # Check that all functions have been wrapped
    for key, task in dsk.items():
        newtask = newdsk[key]
        assert newtask[0].__name__ == "exec_store_wrapper"
        if isiterable(task):
            assert newtask[1:] == task[1:]
        else:
            assert not newtask[1:]

    # Check that the hash files are written and that each
    # filename can be found as a key in the hashchain
    # (the association of hash <-> DAG tasks is not tested)
    hashchainfile = "hashchain.bin"
    filelist = os.listdir(tmpdir)
    assert len(filelist) == len(dsk) + 1 # include hashchain.bin
    assert hashchainfile in filelist
    with open(os.path.join(tmpdir, hashchainfile), "rb") as fid:
        hashchain = pickle.load(fid)
    for filename in filelist:
        if filename != "hashchain.bin":
            assert len(filename) == 36 and filename[-4:] == ".bin"
            assert str.split(filename, ".")[0] in hashchain.keys()


def test_second_run(temporary_directory, dask_dag_generation):
    """
    Function that tests a second run of the graphchain
    optimization function 'gcoptimize'. It checks the
    final result, that that all function calls are
    wrapped - for loading and the the result key has
    no dependencies.
    """
    tmpdir = temporary_directory
    dsk = dask_dag_generation
    newdsk = gcoptimize(dsk, keys=["top1"], cachedir=tmpdir, verbose=True)

    # Check the final result
    result = dask.get(newdsk, ["top1"])
    assert result == (-14,)

    # Check that the functions are wrapped for loading
    for key in dsk.keys():
        newtask = newdsk[key]
        assert type(newtask) is tuple
        assert len(newtask) == 1 # only the loading wrapper
        assert newtask[0].__name__ == "loading_wrapper"

    # Check that there are no dependencies for the top node
    assert not dask.optimization.get_dependencies(newdsk, "top1")


def test_node_changes(temporary_directory, dask_dag_generation):
    """
    Function that tests the functionality of the graphchain in
    the event of changes in the structure of the graph, namely
    by altering the functions/constants associated to the tasks.
    After optimization, the afected nodes should be wrapped in
    a storeand execution wrapper and their dependency lists
    should not be empty.
    """
    tmpdir = temporary_directory
    dsk = dask_dag_generation

    # Replacement function 'goo'
    def goo(*args):
        # hash miss!
        return sum(args) + 1

    # Replacement function 'top'
    def top(argument, argument2):
        # hash miss!
        return argument - argument2

    moddata = {"goo1": (goo, {"goo1", "baz2", "top1"}, (-14,)),
               "top1": (top, {"top1"}, (-14,)),
               "v2": (1000, {"v2", "bar1", "boo1", "baz2", "top1"}, (-1012,))
              }

    for (modkey, (taskobj, affected_nodes, result)) in moddata.items():
        workdsk = dsk.copy()
        if callable(taskobj):
            workdsk[modkey] = (taskobj, *dsk[modkey][1:])
        else:
            workdsk[modkey] = taskobj
        newdsk = gcoptimize(workdsk,
                            keys=["top1"],
                            cachedir=tmpdir,
                            verbose=True)

        assert result == dask.get(newdsk, ["top1"])

        for key, newtask in newdsk.items():
            if callable(taskobj):
                if key in affected_nodes:
                    assert newtask[0].__name__ == "exec_store_wrapper"
                    assert get_dependencies(newdsk, key)
                else:
                    assert newtask[0].__name__ == "loading_wrapper"
                    assert not get_dependencies(newdsk, key)
            else:
                if key in affected_nodes and key == modkey:
                    assert newtask[0].__name__ == "exec_store_wrapper"
                    assert not get_dependencies(newdsk, key)
                elif key in affected_nodes:
                    assert newtask[0].__name__ == "exec_store_wrapper"
                    assert get_dependencies(newdsk, key)
                else:
                    assert newtask[0].__name__ == "loading_wrapper"
                    assert not get_dependencies(newdsk, key)
