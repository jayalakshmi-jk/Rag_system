from __future__ import annotations

from typing import Any

from playwright.sync_api import expect

from tests.e2e.conftest import Stack

EMAIL, PASSWORD = "asha@example.com", "correct-horse-battery"


def fill_signin(page: Any, email: str = EMAIL, password: str = PASSWORD) -> None:
    page.fill("#signin-email", email)
    page.fill("#signin-password", password)
    page.click("#signin-submit")


def test_unauthenticated_visitor_is_sent_to_login(page: Any) -> None:
    page.goto("/app")
    page.wait_for_url("**/login")
    expect(page.locator("#form-signin")).to_be_visible()
    assert page.csp_violations == []
    assert page.page_errors == []


def test_register_then_land_in_workspace_and_stay_logged_in(page: Any, stack: Stack) -> None:
    page.goto("/login")
    page.click("#tab-signup")
    page.fill("#signup-name", "Asha")
    page.fill("#signup-email", EMAIL)
    page.fill("#signup-password", PASSWORD)
    page.click("#signup-submit")
    page.wait_for_url("**/app")
    expect(page.locator("#quota-pill")).to_contain_text("questions left today")
    page.click("#account-btn")
    expect(page.locator("#account-name")).to_contain_text("Asha")
    page.click("#account-close")
    # Reopening the app later: session is persisted, no login needed.
    page.reload()
    expect(page.locator("#quota-pill")).to_contain_text("questions left today")
    page.goto("/login")
    page.wait_for_url("**/app")
    assert page.csp_violations == []
    assert page.page_errors == []


def test_sign_in_and_sign_out(page: Any, stack: Stack) -> None:
    stack.auth.add_user(EMAIL, PASSWORD, "Asha")
    page.goto("/login")
    fill_signin(page)
    page.wait_for_url("**/app")
    page.click("#account-btn")
    page.click("#signout-btn")
    page.wait_for_url("**/login")
    page.goto("/app")
    page.wait_for_url("**/login")


def test_wrong_password_shows_a_friendly_error(page: Any, stack: Stack) -> None:
    stack.auth.add_user(EMAIL, PASSWORD)
    page.goto("/login")
    fill_signin(page, password="wrong-password")
    expect(page.locator("#alert")).to_contain_text("Email or password is incorrect.")
    assert "/login" in page.url


def test_client_side_validation(page: Any) -> None:
    page.goto("/login")
    page.click("#signin-submit")
    expect(page.locator("#form-signin .error").first).to_have_text("Enter a valid email address.")
    page.click("#tab-signup")
    page.click("#signup-submit")
    expect(page.locator("#signup-password ~ .error")).to_have_text("Use at least 8 characters.")


def test_signup_with_email_confirmation_asks_to_check_inbox(page: Any, stack: Stack) -> None:
    stack.auth.require_confirmation = True
    page.goto("/login")
    page.click("#tab-signup")
    page.fill("#signup-name", "Asha")
    page.fill("#signup-email", EMAIL)
    page.fill("#signup-password", PASSWORD)
    page.click("#signup-submit")
    expect(page.locator("#alert")).to_contain_text("Check your email")
    assert "/login" in page.url


def test_duplicate_registration_is_reported(page: Any, stack: Stack) -> None:
    stack.auth.add_user(EMAIL, PASSWORD)
    page.goto("/login")
    page.click("#tab-signup")
    page.fill("#signup-name", "Asha")
    page.fill("#signup-email", EMAIL)
    page.fill("#signup-password", PASSWORD)
    page.click("#signup-submit")
    expect(page.locator("#alert")).to_contain_text("already exists")


def test_delete_account_requires_typing_delete_and_removes_everything(
    page: Any, stack: Stack
) -> None:
    user_id = stack.auth.add_user(EMAIL, PASSWORD, "Asha")
    stack.db.table("conversations").insert({"user_id": user_id, "title": "t"}).execute()
    page.goto("/login")
    fill_signin(page)
    page.wait_for_url("**/app")
    page.click("#account-btn")
    page.click("#delete-btn")
    expect(page.locator("#delete-dialog")).to_be_visible()
    submit = page.locator("#delete-submit")
    expect(submit).to_be_disabled()
    page.fill("#delete-confirm", "delete")
    expect(submit).to_be_disabled()
    page.fill("#delete-confirm", "DELETE")
    expect(submit).to_be_enabled()
    submit.click()
    page.wait_for_url("**/login")
    assert user_id not in stack.db.auth_users
    assert not stack.db.user_has_data(user_id)
    fill_signin(page)  # the fake auth server no longer has a valid profile; the app refuses it
    page.wait_for_timeout(500)
    assert "/app" not in page.url or page.locator("#loading").is_visible()


def test_cancel_keeps_the_account(page: Any, stack: Stack) -> None:
    user_id = stack.auth.add_user(EMAIL, PASSWORD)
    page.goto("/login")
    fill_signin(page)
    page.wait_for_url("**/app")
    page.click("#account-btn")
    page.click("#delete-btn")
    page.click("#delete-cancel")
    expect(page.locator("#delete-dialog")).not_to_be_visible()
    assert user_id in stack.db.auth_users


def test_theme_toggle_persists(page: Any, stack: Stack) -> None:
    stack.auth.add_user(EMAIL, PASSWORD)
    page.goto("/login")
    fill_signin(page)
    page.wait_for_url("**/app")
    page.click("#theme-btn")
    expect(page.locator("#theme-btn")).to_have_text("Theme: light")
    page.click("#theme-btn")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"


def test_pages_have_security_headers_and_no_inline_script(page: Any) -> None:
    response = page.goto("/login")
    csp = response.headers["content-security-policy"]
    assert "'unsafe-inline'" not in csp
    assert "script-src 'self'" in csp
    assert page.locator("script:not([src])").count() == 0
