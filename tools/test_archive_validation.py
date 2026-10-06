import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('catalog_validation', Path(__file__).with_name('validate_catalog.py'))
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class ArchiveValidationTests(unittest.TestCase):
    def test_pointer_validation_keeps_contract_checks(self):
        entry = {
            'id': 'test', 'title': 'Test', 'summary': 'Test', 'category': 'encyclopedia',
            'attribution': 'Test', 'license': 'CC0', 'bundled': False,
            'recordCountEstimate': 1,
            'release': {'version': '2026.10.06', 'schemaVersion': 1,
                        'downloadURL': 'https://example.invalid/missing.gz',
                        'downloadBytes': 100, 'installedBytes': 200,
                        'sha256': 'a' * 64, 'compression': 'gzip', 'publishedAt': '2026-10-06'},
        }
        with patch.object(validator, 'MANIFEST_ONLY', True), patch.object(validator, 'problems', []):
            validator.check_dataset(entry)
            self.assertEqual(validator.problems, [])
            entry['release']['downloadURL'] = 'http://example.invalid/missing.gz'
            validator.check_dataset(entry)
            self.assertTrue(any('must be https' in problem for problem in validator.problems))

    def test_default_validation_still_requires_local_bytes(self):
        import copy
        import json
        entry = copy.deepcopy(json.loads(validator.MANIFEST.read_text())['datasets'][0])
        entry['release']['downloadURL'] = 'https://example.invalid/missing-artifact.gz'
        with patch.object(validator, 'MANIFEST_ONLY', False), patch.object(validator, 'problems', []):
            validator.check_dataset(entry)
            self.assertTrue(any('is not in dist/' in problem for problem in validator.problems))


if __name__ == '__main__':
    unittest.main()
