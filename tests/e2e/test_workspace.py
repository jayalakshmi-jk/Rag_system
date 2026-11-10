"""Browser tests for the workspace: upload, chat streaming, citations, history, mobile."""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Stack
from tests.pdfs import make_pdf

EMAIL, PASSWORD = "asha@example.com", "correct-horse-battery"
POLICY = (
    "Refund policy\n"
    "Customers can request a refund within 30 days of purchase if the product is unused. "
    "Refunds are paid back to the original payment method within five business days."
)


@pytest.fixture
def workspace(page: Any, stack: Stack) -> Any:
    stack.auth.add_user(EMAIL, PASSWORD, "Asha")
    page.goto("/login")
    page.fill("#signin-email", EMAIL)
    page.fill("#signin-password", PASSWORD)
    page.click("#signin-submit")
    page.wait_for_url("**/app")
    expect(page.locator("#shell")).to_be_visible()
    return page


def upload_pdf(page: Any, name: str = "policy.pdf", text: str = POLICY) -> None:
    page.set_input_files(
        "#file-input", {"name": name, "mimeType": "application/pdf", "buffer": make_pdf([text])}
    )


def test_first_run_shows_onboarding_and_disables_sending(workspace: Any) -> None:
    expect(workspace.locator("#onboarding")).to_be_visible()
    expect(workspace.locator("#doc-empty")).to_be_visible()
    workspace.fill("#question", "hello")
    expect(workspace.locator("#send-btn")).to_be_disabled()
    assert workspace.csp_violations == []
    assert workspace.page_errors == []


def test_upload_shows_progress_then_ready_and_unlocks_chat(workspace: Any) -> None:
    upload_pdf(workspace)
    item = workspace.locator(".doc", has_text="policy")
    expect(item).to_be_visible()
    expect(item.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)
    expect(workspace.locator("#doc-count")).to_have_text("1 of 1 ready")
    expect(workspace.locator("#starter")).to_be_visible()
    expect(workspace.locator("#scope")).to_have_text("Asking all 1 document")


def test_chat_streams_an_answer_with_clickable_citation_and_highlighted_source(
    workspace: Any, stack: Stack
) -> None:
    stack.llm.answer = "Refunds are available within 30 days of purchase [1]."
    upload_pdf(workspace)
    expect(workspace.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)

    workspace.fill("#question", "What is the refund policy within 30 days?")
    workspace.keyboard.press("Enter")
    answer = workspace.locator(".msg-assistant .bubble").last
    expect(answer).to_contain_text("within 30 days of purchase", timeout=10_000)
    expect(workspace.locator(".msg-user .bubble")).to_have_text(
        "What is the refund policy within 30 days?"
    )
    cite = answer.locator("button.cite")
    expect(cite).to_have_text("1")
    expect(workspace.locator(".source-chips .chip").first).to_contain_text("policy · p.1")

    cite.click()
    expect(workspace.locator("#source-title")).to_have_text("policy · page 1")
    expect(workspace.locator("#source-body mark")).to_contain_text("Refund policy")
    expect(workspace.locator("#source-meta")).to_have_text("Page 1 of 1")

    # Conversation was saved, titled, and shown in the sidebar; quota went down.
    expect(workspace.locator(".conv-open").first).to_have_text("Photosynthesis basics")
    expect(workspace.locator("#quota-pill")).to_contain_text("49 questions left today")
    expect(workspace.locator(".suggestions .chip").first).to_be_visible()
    assert workspace.csp_violations == []
    assert workspace.page_errors == []


def test_shift_enter_adds_a_newline_instead_of_sending(workspace: Any, stack: Stack) -> None:
    upload_pdf(workspace)
    expect(workspace.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)
    workspace.fill("#question", "line one")
    workspace.keyboard.press("Shift+Enter")
    workspace.keyboard.type("line two")
    assert workspace.input_value("#question") == "line one\nline two"
    assert workspace.locator(".msg").count() == 0


def test_html_in_answers_and_questions_is_rendered_as_text(workspace: Any, stack: Stack) -> None:
    stack.llm.answer = (
        "<img src=x onerror=window.__pwned=1> **bold** <script>window.__pwned=2</script>"
    )
    upload_pdf(workspace)
    expect(workspace.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)
    workspace.fill("#question", "<b>refund</b> <script>window.__pwned=3</script> policy")
    workspace.keyboard.press("Enter")
    expect(workspace.locator(".msg-assistant .bubble strong")).to_have_text("bold", timeout=10_000)
    assert workspace.evaluate("window.__pwned") is None
    assert workspace.locator(".msg-assistant img").count() == 0
    expect(workspace.locator(".msg-user .bubble")).to_contain_text("<b>refund</b>")


def test_reopen_rename_search_and_delete_conversations(workspace: Any, stack: Stack) -> None:
    upload_pdf(workspace)
    expect(workspace.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)
    workspace.fill("#question", "What is the refund policy?")
    workspace.keyboard.press("Enter")
    expect(workspace.locator(".conv-open").first).to_have_text(
        "Photosynthesis basics", timeout=10_000
    )

    workspace.click("#new-chat")
    expect(workspace.locator(".msg")).to_have_count(0)
    workspace.click(".conv-open")
    expect(workspace.locator(".msg")).to_have_count(2)
    expect(workspace.locator(".group-title").first).to_have_text("Today")

    workspace.click("button[aria-label^='Rename']")
    workspace.fill("#prompt-input", "Refund questions")
    workspace.click("#prompt-form button[type=submit]")
    expect(workspace.locator(".conv-open")).to_have_text("Refund questions")

    workspace.fill("#conv-search", "refund")
    expect(workspace.locator(".conv-open")).to_have_count(1)
    workspace.fill("#conv-search", "nothing-matches-this")
    expect(workspace.locator("#conv-empty")).to_contain_text("No conversations match")
    workspace.fill("#conv-search", "")
    expect(workspace.locator(".conv-open")).to_have_count(1, timeout=5_000)

    workspace.click("button[aria-label^='Delete']")
    workspace.click("#confirm-ok")
    expect(workspace.locator(".conv-open")).to_have_count(0)
    expect(workspace.locator(".msg")).to_have_count(0)


def test_feedback_buttons_toggle(workspace: Any, stack: Stack) -> None:
    upload_pdf(workspace)
    expect(workspace.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)
    workspace.fill("#question", "What is the refund policy?")
    workspace.keyboard.press("Enter")
    up = workspace.locator("button[aria-label='Good answer']")
    expect(up).to_be_visible(timeout=10_000)
    up.click()
    expect(up).to_have_attribute("aria-pressed", "true")
    assert any(r["rating"] == "up" for r in stack.db.tables["message_feedback"])
    up.click()
    expect(up).to_have_attribute("aria-pressed", "false")
    workspace.click("button[aria-label='Bad answer']")
    workspace.select_option("#reason-select", "wrong_citation")
    workspace.click("#reason-form button[type=submit]")
    down = workspace.locator("button[aria-label='Bad answer']")
    expect(down).to_have_attribute("aria-pressed", "true")
    assert stack.db.tables["message_feedback"][0]["reason"] == "wrong_citation"


def test_failed_upload_shows_the_reason_and_deleting_works(workspace: Any) -> None:
    upload_pdf(workspace, "scan.pdf", "")
    item = workspace.locator(".doc", has_text="scan")
    expect(item.locator(".doc-meta")).to_contain_text(
        "OCR is not currently supported", timeout=10_000
    )
    expect(workspace.locator("#onboarding")).to_be_visible()
    item.locator("button[aria-label^='Delete']").click()
    workspace.click("#confirm-ok")
    expect(workspace.locator(".doc")).to_have_count(0)


def test_duplicate_upload_asks_before_adding_again(workspace: Any) -> None:
    upload_pdf(workspace)
    expect(workspace.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)
    upload_pdf(workspace, "policy-copy.pdf")
    expect(workspace.locator("#confirm-title")).to_have_text("This document already exists.")
    workspace.click("#confirm-cancel")
    expect(workspace.locator(".doc")).to_have_count(1)
    upload_pdf(workspace, "policy-copy.pdf")
    workspace.click("#confirm-ok")
    expect(workspace.locator(".doc")).to_have_count(2, timeout=10_000)


def test_unsupported_file_is_rejected_in_the_browser(workspace: Any) -> None:
    workspace.set_input_files(
        "#file-input", {"name": "slides.pptx", "mimeType": "application/zip", "buffer": b"PK"}
    )
    expect(workspace.locator(".toast")).to_contain_text("Only PDF, TXT and MD")
    expect(workspace.locator(".doc")).to_have_count(0)


def test_selecting_a_document_limits_the_scope(workspace: Any) -> None:
    upload_pdf(workspace, "one.pdf", POLICY)
    upload_pdf(workspace, "two.pdf", "Shipping takes seven days. " * 10)
    expect(workspace.locator("#doc-count")).to_have_text("2 of 2 ready", timeout=10_000)
    workspace.locator(".doc", has_text="two").locator("input[type=checkbox]").check()
    expect(workspace.locator("#scope")).to_have_text("Asking 1 selected document")


def test_quota_exceeded_is_shown_in_the_chat(workspace: Any, stack: Stack) -> None:
    upload_pdf(workspace)
    expect(workspace.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)
    user_id = next(iter(stack.db.auth_users))
    for _ in range(50):
        stack.db.rpc("consume_question", {"p_user": user_id, "p_limit": 50}).execute()
    workspace.fill("#question", "What is the refund policy?")
    workspace.keyboard.press("Enter")
    expect(workspace.locator(".msg-error")).to_contain_text("used all 50 questions", timeout=10_000)


def test_provider_error_shows_a_retry_button(workspace: Any, stack: Stack) -> None:
    from tests.fakes.llm import llm_down

    upload_pdf(workspace)
    expect(workspace.locator(".doc-meta")).to_contain_text("1 page", timeout=10_000)
    stack.llm.fail_before_first_token = llm_down()
    workspace.fill("#question", "What is the refund policy?")
    workspace.keyboard.press("Enter")
    expect(workspace.locator(".msg-error")).to_contain_text("busy", timeout=10_000)
    stack.llm.fail_before_first_token = None
    workspace.click("text=Try again")
    expect(workspace.locator(".msg-assistant .bubble").last).to_contain_text(
        "Photosynthesis", timeout=10_000
    )


def test_dark_theme_and_mobile_layout(browser: Any, stack: Stack) -> None:
    stack.auth.add_user(EMAIL, PASSWORD, "Asha")
    context = browser.new_context(
        base_url=stack.base_url, viewport={"width": 390, "height": 800}, color_scheme="dark"
    )
    page = context.new_page()
    page.goto("/login")
    page.fill("#signin-email", EMAIL)
    page.fill("#signin-password", PASSWORD)
    page.click("#signin-submit")
    page.wait_for_url("**/app")
    expect(page.locator(".mobile-nav")).to_be_visible()
    expect(page.locator("#center")).to_be_visible()
    expect(page.locator("#left")).not_to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.click(".mobile-nav [data-view=right]")
    expect(page.locator("#right")).to_be_visible()
    page.click(".mobile-nav [data-view=left]")
    expect(page.locator("#left")).to_be_visible()
    background = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert background == "rgb(20, 20, 18)"
    context.close()


def test_landing_page_is_public_and_links_to_signup(page: Any) -> None:
    page.goto("/")
    expect(page.locator("h1")).to_contain_text("Ask your documents")
    page.click("#cta-start")
    page.wait_for_url("**/login#signup")
    expect(page.locator("#form-signup")).to_be_visible()
    assert page.csp_violations == []
    assert page.page_errors == []
