"""
tests/test_source_class.py
--------------------------
`ingestion.source_class.is_reviewable_code` — the shared "is this real
first-party source a code-quality reviewer should nitpick?" gate.
"""
from ingestion.source_class import is_reviewable_code


def test_real_source_is_reviewable():
    assert is_reviewable_code("src/main/java/com/x/Foo.java")
    assert is_reviewable_code("app/service.py")
    assert is_reviewable_code("pkg/handler.go")
    assert is_reviewable_code("Main.kt")


def test_dependency_manifests_are_not_reviewable():
    assert not is_reviewable_code("pom.xml")
    assert not is_reviewable_code("cswservice-core/pom.xml")
    assert not is_reviewable_code("build.gradle")
    assert not is_reviewable_code("package.json")
    assert not is_reviewable_code("package-lock.json")
    assert not is_reviewable_code("requirements.txt")


def test_markup_config_data_are_not_reviewable():
    assert not is_reviewable_code("deploy/values.yaml")
    assert not is_reviewable_code("src/main/resources/application.properties")
    assert not is_reviewable_code("web.xml")
    assert not is_reviewable_code("db/migration/V3__add_col.sql")
    assert not is_reviewable_code("README.md")
    assert not is_reviewable_code("notes.txt")


def test_test_files_are_not_reviewable():
    assert not is_reviewable_code(
        "cswservice-core/src/test/java/com/uob/cus/csw/integration/"
        "processors/crs/CrsEnquiryResponseProcessorTest.java"
    )
    assert not is_reviewable_code("tests/test_thing.py")
    assert not is_reviewable_code("pkg/handler_test.go")
    assert not is_reviewable_code("src/__tests__/widget.spec.ts")


def test_empty_path():
    assert not is_reviewable_code("")
