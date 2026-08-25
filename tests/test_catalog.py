import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from diagrams import Node


# Load the owned module directly so this unit test does not depend on optional
# imports exposed by the package-level __init__.py.
_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "diagram-grill"
    / "scripts"
    / "diagram_grill"
    / "catalog.py"
)
_CATALOG_SPEC = importlib.util.spec_from_file_location(
    "diagram_grill_catalog_under_test", _CATALOG_PATH
)
if _CATALOG_SPEC is None or _CATALOG_SPEC.loader is None:
    raise RuntimeError("could not load catalog module under test")
catalog = importlib.util.module_from_spec(_CATALOG_SPEC)
sys.modules[_CATALOG_SPEC.name] = catalog
_CATALOG_SPEC.loader.exec_module(catalog)


class CatalogDiscoveryTests(unittest.TestCase):
    def setUp(self):
        catalog.discover_node_catalog.cache_clear()

    def tearDown(self):
        catalog.discover_node_catalog.cache_clear()

    def test_discovers_real_public_node_subclasses_in_sorted_order(self):
        entries = catalog.discover_node_catalog()

        self.assertIsInstance(entries, tuple)
        self.assertGreater(len(entries), 1000)
        self.assertEqual(
            [entry.import_path for entry in entries],
            sorted(entry.import_path for entry in entries),
        )

        by_path = {entry.import_path: entry for entry in entries}
        ec2 = by_path["diagrams.aws.compute.EC2"]
        self.assertEqual(ec2.provider, "aws")
        self.assertEqual(ec2.category, "compute")
        self.assertEqual(ec2.class_name, "EC2")
        self.assertIn("aws", ec2.search_text.split())
        self.assertIn("compute", ec2.search_text.split())
        self.assertIn("ec2", ec2.search_text.split())

    def test_excludes_custom_internal_and_private_symbols(self):
        entries = catalog.discover_node_catalog()

        self.assertFalse(
            any(entry.import_path.startswith("diagrams.custom") for entry in entries)
        )
        self.assertFalse(any(entry.provider in {"base", "cli"} for entry in entries))
        self.assertFalse(any(entry.class_name.startswith("_") for entry in entries))
        self.assertNotIn("diagrams.Node", {entry.import_path for entry in entries})

    def test_every_entry_has_the_exact_ir_icon_path_shape(self):
        entries = catalog.discover_node_catalog()

        self.assertTrue(entries)
        self.assertTrue(all(entry.category for entry in entries))
        self.assertTrue(
            all(len(entry.import_path.split(".")) == 4 for entry in entries)
        )

    def test_discovery_is_cached_once_per_process(self):
        with mock.patch.object(
            catalog,
            "_discover_node_catalog_uncached",
            wraps=catalog._discover_node_catalog_uncached,
        ) as discover_uncached:
            first = catalog.discover_node_catalog()
            second = catalog.discover_node_catalog()

        self.assertIs(first, second)
        discover_uncached.assert_called_once_with()

    def test_version_mismatch_fails_before_discovery(self):
        with mock.patch.object(
            catalog.importlib_metadata,
            "version",
            return_value="0.26.0",
        ), mock.patch.object(catalog.importlib, "import_module") as import_module:
            with self.assertRaisesRegex(
                catalog.DiagramsVersionError,
                r"requires diagrams==0\.25\.1; found 0\.26\.0",
            ):
                catalog.discover_node_catalog()
        import_module.assert_not_called()


class CatalogSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog.discover_node_catalog.cache_clear()
        catalog.discover_node_catalog()

    def test_searches_provider_category_and_class_tokens(self):
        results = catalog.search_node_catalog(
            "aws compute ec2",
            provider="AWS",
            category="Compute",
            limit=8,
        )

        self.assertTrue(results)
        self.assertLessEqual(len(results), 8)
        self.assertEqual(results[0].import_path, "diagrams.aws.compute.EC2")
        self.assertTrue(all(entry.provider == "aws" for entry in results))
        self.assertTrue(all(entry.category == "compute" for entry in results))
        self.assertEqual(
            results,
            catalog.search_node_catalog(
                "aws compute ec2",
                provider="aws",
                category="compute",
                limit=8,
            ),
        )

    def test_search_splits_camel_case_class_tokens(self):
        results = catalog.search_node_catalog(
            "service bus",
            provider="azure",
            category="integration",
            limit=20,
        )

        self.assertIn(
            "diagrams.azure.integration.ServiceBus",
            {entry.import_path for entry in results},
        )

    def test_exact_public_symbol_ranks_ahead_of_matching_alias_target(self):
        results = catalog.search_node_catalog(
            "ec2",
            provider="aws",
            category="compute",
            limit=100,
        )
        paths = [entry.import_path for entry in results]

        self.assertIn("diagrams.aws.compute.AMI", paths)
        self.assertLess(
            paths.index("diagrams.aws.compute.EC2"),
            paths.index("diagrams.aws.compute.AMI"),
        )

    def test_empty_query_lists_filtered_entries_with_a_limit(self):
        results = catalog.search_node_catalog(
            provider="generic",
            category="network",
            limit=3,
        )

        self.assertEqual(len(results), 3)
        self.assertEqual(
            [entry.import_path for entry in results],
            sorted(entry.import_path for entry in results),
        )

    def test_rejects_invalid_limits_and_non_token_queries(self):
        with self.assertRaises(ValueError):
            catalog.search_node_catalog("ec2", limit=0)
        with self.assertRaises(TypeError):
            catalog.search_node_catalog("ec2", limit=True)
        with self.assertRaises(ValueError):
            catalog.search_node_catalog("---")


class CatalogResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog.discover_node_catalog.cache_clear()
        catalog.discover_node_catalog()

    def test_resolves_only_an_exact_catalog_import_path(self):
        ec2 = catalog.resolve_node("diagrams.aws.compute.EC2")

        self.assertTrue(issubclass(ec2, Node))
        self.assertEqual(ec2.__module__, "diagrams.aws.compute")

        with self.assertRaises(catalog.UnknownNodeError):
            catalog.resolve_node("diagrams.aws.compute.ec2")
        with self.assertRaises(catalog.UnknownNodeError):
            catalog.resolve_node("diagrams.aws.compute.DoesNotExist")
        with self.assertRaises(catalog.UnknownNodeError):
            catalog.resolve_node("diagrams.custom.Custom")

    def test_generic_fallback_is_the_verified_neutral_blank_node(self):
        fallback = catalog.generic_fallback()

        self.assertEqual(
            fallback.import_path,
            catalog.GENERIC_FALLBACK_IMPORT_PATH,
        )
        self.assertEqual(fallback.provider, "generic")
        self.assertEqual(fallback.category, "blank")
        self.assertEqual(fallback.class_name, "Blank")
        self.assertTrue(issubclass(catalog.resolve_node(fallback.import_path), Node))


if __name__ == "__main__":
    unittest.main()
