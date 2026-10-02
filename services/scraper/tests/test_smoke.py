def test_package_imports():
    import cageops_common
    import cageops_scraper

    assert cageops_scraper is not None
    assert cageops_common is not None
