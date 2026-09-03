# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

import frappe

from afaa.tests.utils import AFAATestSuite, boot_strap_test_master_data


class TestAIModel(AFAATestSuite):
	def test_id_uses_provider_and_model_id(self):
		provider = frappe.get_doc("AI Provider", boot_strap_test_master_data.provider)
		model_id = f"test-model-{frappe.generate_hash(length=8).lower()}"
		model = frappe.get_doc(
			{
				"doctype": "AI Model",
				"provider": provider.name,
				"model_id": model_id,
				"available": 1,
			}
		).insert()

		self.assertEqual(model.name, f"{provider.name}:{model_id}")
		self.assertEqual(model.model_name, model.name)
