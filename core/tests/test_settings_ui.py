from html.parser import HTMLParser

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.tests import test_workflow


class Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs = []
        self.form_depth = 0
        self.nested = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.form_depth += 1
            self.nested |= self.form_depth > 1
        if tag in {"input", "select"}:
            self.inputs.append(attrs)

    def handle_endtag(self, tag):
        if tag == "form":
            self.form_depth -= 1


@override_settings(SECURE_SSL_REDIRECT=False, DASHBOARD_RETRY_USER_ID=1)
class SettingsUITests(TestCase):
    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        owner = get_user_model().objects.create_user(username="owner", is_superuser=True)
        binding = override_settings(DASHBOARD_RETRY_USER_ID=owner.pk)
        binding.enable()
        self.addCleanup(binding.disable)
        self.client.force_login(owner)

    def test_repository_management_and_compact_policy_controls(self):
        response = self.client.get("/settings/")
        self.assertContains(response, "Add repository")
        self.assertContains(response, "Pause")
        self.assertContains(response, 'name="repository-policies"')
        self.assertNotContains(response, "after a complete review and passing required CI")
        controls = Controls()
        controls.feed(response.content.decode())
        self.assertFalse(controls.nested)
        fields = {item.get("name"): item for item in controls.inputs}
        for name in [
            "concurrent_pr_reviews",
            "cli_account_concurrency",
            f"auto_approve_{self.repo.pk}",
            f"auto_merge_{self.repo.pk}",
            f"related_item_limit_{self.repo.pk}",
        ]:
            self.assertEqual(fields[name].get("form"), "service-settings")
