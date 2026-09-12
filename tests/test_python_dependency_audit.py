from scripts.audit_python_dependencies import requires_license_review


def test_lgpl_is_not_rejected_as_bare_gpl() -> None:
    assert not requires_license_review("LGPL-3.0-only")
    assert not requires_license_review("GNU Lesser General Public License v3 (LGPLv3)")


def test_gpl_and_agpl_require_review() -> None:
    assert requires_license_review("GPL-3.0-only")
    assert requires_license_review("GNU General Public License v3 (GPLv3)")
    assert requires_license_review("AGPL-3.0-only")


def test_permissive_license_does_not_require_review() -> None:
    assert not requires_license_review("MIT License")
    assert not requires_license_review("Apache Software License")
