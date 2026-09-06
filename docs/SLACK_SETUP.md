# Slack HITL connector — setup

One-time steps to enable the optional Slack approval connector. Nothing here is required for
the app to run — without these env vars, the Connectors tab shows the Slack toggle greyed out
and the app behaves exactly as it does today.

To configure a workspace:

1. Create an app at <https://api.slack.com/apps> in your workspace.
2. **OAuth & Permissions** → Bot Token Scopes → add `chat:write`. Click **Install to
   Workspace**, then copy the **Bot User OAuth Token** (`xoxb-...`).
3. **Basic Information** → copy the **Signing Secret**.
4. Create (or choose) the channel approvals should post to, invite the bot
   (`/invite @YourBotName`), then open the channel details and copy its **Channel ID**
   (`C...`).
5. **Interactivity & Shortcuts** → toggle on → set **Request URL** to
   `https://<your-tunnel>/api/slack/interactions`.
6. For local development, expose your backend with `ngrok http 8000` and use the printed
   `https://*.ngrok.io` URL as `<your-tunnel>` above.
7. Set these three variables in `backend/.env` (see `.env.example`):

   ```env
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_SIGNING_SECRET=...
   SLACK_APPROVALS_CHANNEL_ID=C...
   ```

8. Restart the backend, open the app's **Connectors** tab, and click the Slack toggle to
   **Enabled**.
9. Request a booking in the app — the approval message should appear in the configured
   channel within a few seconds.

## Why this adapter is intentionally narrow

`app/adapters/slack_hitl.py` hand-rolls Slack signature verification and Block Kit message
building because the implemented scope is one Slack approval message and one signed callback.
Adding a multi-platform abstraction would increase the dependency and configuration surface
without serving another current connector. The `notify_pending_approval`, `resolve_approve`, and
`resolve_reject` boundary keeps the Slack-specific code isolated if that scope changes.
