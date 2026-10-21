import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from gnsis.service.stripe_checkout import create_checkout_session


def _settings(**kw):
    base = dict(
        stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x",
        frontend_url="https://gnsis.studio/", default_currency="USD",
        topup_min_usd="5.00", topup_max_usd="500.00",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class CheckoutTests(unittest.TestCase):
    def test_session_is_attributed_to_the_workspace(self):
        seen = {}

        def post(url, form, key):
            seen.update(url=url, form=form, key=key)
            return {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"}

        out = create_checkout_session(_settings(), "ws-1", "25", post=post)
        self.assertEqual(out, {"id": "cs_1", "url": "https://checkout.stripe.com/c/cs_1"})
        f = seen["form"]
        self.assertEqual(f["metadata[workspace_id]"], "ws-1")
        self.assertEqual(f["line_items[0][price_data][unit_amount]"], "2500")
        self.assertEqual(f["mode"], "payment")
        self.assertEqual(f["success_url"], "https://gnsis.studio/dashboard?topup=success")
        self.assertEqual(seen["key"], "sk_test_x")
        # Only the checkout session carries the workspace; payment intents must not,
        # or payment_intent.succeeded would credit the same payment a second time.
        self.assertFalse(any(k.startswith("payment_intent_data") for k in f))

    def test_amount_bounds(self):
        for bad in ("1", "501", "abc"):
            with self.assertRaises(HTTPException) as cm:
                create_checkout_session(_settings(), "ws-1", bad, post=lambda *a: {})
            self.assertEqual(cm.exception.status_code, 422)

    def test_unconfigured_is_503(self):
        with self.assertRaises(HTTPException) as cm:
            create_checkout_session(_settings(stripe_secret_key=None), "ws-1", "25", post=lambda *a: {})
        self.assertEqual(cm.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
