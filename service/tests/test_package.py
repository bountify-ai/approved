import approved


def test_package_has_version() -> None:
    assert approved.__version__ == "0.1.0"
