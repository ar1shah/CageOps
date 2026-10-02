def test_package_imports():
    import cageops_common
    import cageops_worker

    assert cageops_worker is not None
    assert cageops_common is not None
