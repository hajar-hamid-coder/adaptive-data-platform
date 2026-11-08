import adp


def test_package_exposes_version() -> None:
    assert adp.__version__ == "0.1.0"
