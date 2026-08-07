def test_package_imports_and_has_version():
    import devflow_delegation

    assert isinstance(devflow_delegation.__version__, str)
    assert devflow_delegation.__version__ == "1.0.0"
