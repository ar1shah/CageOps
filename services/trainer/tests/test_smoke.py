def test_package_imports():
    import cageops_common
    import cageops_trainer

    assert cageops_trainer is not None
    assert cageops_common is not None
