import logging
import streamlit as st
import urllib.request
import urllib.parse
import json

logger = logging.getLogger(__name__)

def render_billing():
    st.header("SaaS Subscription & Billing")

    active_dep_id = st.session_state.get("active_deployment_id")
    if not active_dep_id:
        st.info("Billing management is only available for SaaS/cloud deployments.")
        return

    client = st.session_state.get("api_client")
    if not client:
        st.error("API client not initialized.")
        return

    # Fetch billing and usage stats
    try:
        data = client.get_cloud_usage(active_dep_id)
    except Exception as e:
        st.error(f"Failed to fetch billing status from API: {e}")
        return

    dep = data.get("deployment", {})
    sub = data.get("subscription")
    plan = data.get("plan", {})
    usage_history = data.get("usage", [])
    invoices = data.get("invoices", [])

    # Get active daily usage
    daily_calls = 0
    if usage_history:
        # Get first entry (usually today)
        daily_calls = usage_history[0].get("mcp_calls", 0) + usage_history[0].get("rest_calls", 0)

    # Renders active plan & limits
    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader("Active Plan Details")
        st.markdown(f"**Current Plan:** `{plan.get('name', 'Free Tier')}`")
        if sub:
            st.markdown(f"**Subscription ID:** `{sub.get('subscription_id')}`")
            st.markdown(f"**Stripe Reference:** `{sub.get('stripe_sub_id')}`")
            import datetime
            try:
                dt_str = datetime.datetime.fromtimestamp(int(sub.get("current_period_end"))).strftime("%Y-%m-%d")
                st.markdown(f"**Current Period Ends:** `{dt_str}`")
            except Exception:
                pass
        else:
            st.markdown("*No active cloud subscription found (defaulting to Free limits)*")

    with col2:
        st.subheader("Plan Limits")
        max_calls = plan.get("max_mcp_calls_per_day", 1000)
        st.metric("Max Daily Calls", f"{max_calls:,}")
        st.metric("Max Storage", f"{plan.get('max_storage_mb', 50)} MB")
        st.metric("Seats Limit", f"{plan.get('max_seats', 1)}")

    st.divider()

    # Renders progress bar usage meters
    st.subheader("Usage Meters")
    usage_pct = min(1.0, daily_calls / max_calls) if max_calls > 0 else 0.0
    st.progress(usage_pct, text=f"Daily API Calls: {daily_calls:,} / {max_calls:,} ({usage_pct:.1%})")

    if usage_pct >= 1.0:
        st.error("⚠️ Plan limits exceeded. Write requests may be rejected until upgrade or reset.")
    elif usage_pct >= 0.8:
        st.warning("⚠️ Approaching daily call limit. Consider upgrading.")

    st.divider()

    # Plan upgrading
    st.subheader("Manage Subscription Plan")
    plans = ["free", "pro", "enterprise"]
    selected_plan = st.radio("Choose a plan tier to upgrade/downgrade:", plans, index=plans.index(plan.get("id", "free")), horizontal=True)

    if selected_plan != plan.get("id", "free"):
        if st.button(f"Upgrade to {selected_plan.capitalize()}"):
            try:
                res = client.create_cloud_checkout(active_dep_id, selected_plan)
                checkout_url = res.get("checkout_url")
                session_id = res.get("session_id")
                st.success(f"Checkout Session Created! Redirect URL: {checkout_url}")
                
                # Render a simulation trigger button
                st.info("Simulating Payment webhook invocation:")
                if st.button("Trigger Mock Stripe Webhook"):
                    # Invoke webhook via API client's base URL
                    webhook_url = f"{client.base_url.rstrip('/')}/api/v1/cloud/webhooks/stripe"
                    payload = {
                        "type": "checkout.session.completed",
                        "data": {
                            "object": {
                                "client_reference_id": active_dep_id,
                                "metadata": {
                                    "plan_id": selected_plan
                                },
                                "subscription": f"sub_stripe_{session_id}"
                            }
                        }
                    }
                    req = urllib.request.Request(
                        webhook_url,
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST"
                    )
                    with urllib.request.urlopen(req) as resp:
                        webhook_res = json.loads(resp.read().decode())
                    st.success(f"Webhook response: {webhook_res}")
                    st.rerun()
            except Exception as e:
                st.error(f"Checkout creation failed: {e}")

    st.divider()

    # Invoice history
    st.subheader("Invoice History")
    if invoices:
        import pandas as pd
        invoice_rows = []
        for inv in invoices:
            import datetime
            try:
                dt_str = datetime.datetime.fromtimestamp(int(inv.get("created_at"))).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt_str = str(inv.get("created_at"))
            invoice_rows.append({
                "Invoice ID": inv["invoice_id"],
                "Amount": f"${inv['amount_cents'] / 100:.2f}",
                "Status": inv["status"].upper(),
                "Billing Date": dt_str
            })
        st.dataframe(pd.DataFrame(invoice_rows), use_container_width=True)
    else:
        st.info("No invoice history available.")
