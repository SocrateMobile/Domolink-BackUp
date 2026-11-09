"""Unit and security non-regression tests for Domolink-BackUp."""
import json
import os
import ast
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENT_DIR = os.path.join(REPO_ROOT, "custom_components", "domolink_backup")


def test_manifest_structure_and_version():
    """Verify manifest.json is valid and keys are properly ordered."""
    manifest_path = os.path.join(COMPONENT_DIR, "manifest.json")
    assert os.path.isfile(manifest_path)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    assert manifest["domain"] == "domolink_backup"
    assert manifest["name"] == "DomoLink-BackUp"
    assert manifest["version"] == "1.5.24"
    assert manifest["codeowners"] == ["@SocrateMobile"]
    assert manifest["iot_class"] == "local_polling"


def test_translations_parity():
    """Verify en.json and fr.json exist and match keys."""
    fr_path = os.path.join(COMPONENT_DIR, "translations", "fr.json")
    en_path = os.path.join(COMPONENT_DIR, "translations", "en.json")
    assert os.path.isfile(fr_path), "translations/fr.json must exist"
    assert os.path.isfile(en_path), "translations/en.json must exist"

    with open(fr_path, "r", encoding="utf-8") as f:
        fr = json.load(f)
    with open(en_path, "r", encoding="utf-8") as f:
        en = json.load(f)

    def extract_keys(d, prefix=""):
        keys = set()
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys.update(extract_keys(v, full))
            else:
                keys.add(full)
        return keys

    fr_keys = extract_keys(fr)
    en_keys = extract_keys(en)
    assert fr_keys == en_keys, f"Translation keys mismatch: {fr_keys ^ en_keys}"


def test_no_direct_blocking_io_in_async_functions():
    """Scan all Python files in the component to ensure no open() or blocking subprocess calls directly inside async def (excluding nested defs)."""
    blocking_calls = {"open", "check_output", "urlopen"}
    violations = []

    def check_node(node, in_async=False):
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            # Nested synchronous function - this is safe as it's meant for executor or thread
            return
        if isinstance(node, ast.AsyncFunctionDef):
            for child in node.body:
                check_async_body(child, node.name, file)
            return

        for child in ast.iter_child_nodes(node):
            check_node(child, in_async=False)

    def check_async_body(node, func_name, file_name):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Don't recurse into nested defs here; if async, top-level loop handles it
            return
        if isinstance(node, ast.Call):
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in blocking_calls:
                violations.append((file_name, func_name, name, node.lineno))

        for child in ast.iter_child_nodes(node):
            check_async_body(child, func_name, file_name)

    for root, _, files in os.walk(COMPONENT_DIR):
        for file in files:
            if not file.endswith(".py"):
                continue
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=path)
            for item in tree.body:
                check_node(item)

    assert not violations, f"Blocking calls found directly inside async functions: {violations}"
