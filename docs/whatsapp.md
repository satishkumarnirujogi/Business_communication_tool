# Connecting WhatsApp

Patients can write to the clinic's WhatsApp number. The assistant answers there just as it does in the web
chat, with the same safety rules: emergency detection, a confirmation before booking, and staff take-over.
Reminders for appointments booked on WhatsApp are sent there too.

You need the clinic's own **WhatsApp Business** number and one of two providers:

| | Twilio | Meta WhatsApp Cloud API |
|---|---|---|
| Setup | Easiest; a sandbox lets you test in minutes | A Meta developer app and a Business account |
| Cost | Twilio fee + Meta fee per conversation | Meta fee per conversation only |
| Settings | `TWILIO_*` | `WHATSAPP_*` |

Everything below can be entered in **Admin → Settings → Channels** instead of `.env`.

## 1. Give the server a public address

WhatsApp has to reach the assistant over HTTPS. Set `PUBLIC_BASE_URL` to that address, e.g.
`https://assistant.your-clinic.example`. Without a trailing slash, and exactly as the provider will call it:
Twilio signs the full URL, so a mismatch makes every message fail the signature check.

To try it on your own computer, run a tunnel such as `cloudflared tunnel --url http://localhost:8000` or
`ngrok http 8000`, and use the https address it prints.

## 2a. Twilio

1. In the Twilio console, open **Messaging → Try it out → Send a WhatsApp message** (the sandbox), or connect
   your own number under **Messaging → Senders → WhatsApp senders**.
2. Copy the **Account SID** and **Auth token** from the console's home page.
3. In Settings → Channels choose **Twilio**, then enter the SID, the token, and the WhatsApp number in
   international format (`+4930…`). Save.
4. Copy the **webhook URL** shown on that page (`…/webhooks/whatsapp/twilio`). In Twilio, paste it into
   **When a message comes in** for your sender or the sandbox, with method **POST**.
5. Send "Hello" from your phone. The conversation appears under **Conversations** with a WhatsApp icon.

## 2b. Meta WhatsApp Cloud API

1. At developers.facebook.com create an app of type **Business** and add the **WhatsApp** product.
2. Under **WhatsApp → API Setup**, note the **Phone number ID**. Create a permanent access token: add a system
   user in Business settings, give it the app, and generate a token with `whatsapp_business_messaging`. The
   temporary token on the setup page expires after 24 hours.
3. Under **App settings → Basic**, copy the **App secret**.
4. In Settings → Channels choose **Meta**. Enter the token, the phone number ID and the app secret, plus a
   **verify token**: any long random text you make up. Save.
5. In Meta, open **WhatsApp → Configuration → Webhook → Edit**. Paste the webhook URL
   (`…/webhooks/whatsapp/meta`) and the same verify token, then **Verify and save**. Subscribe to the
   **messages** field.

## 3. Reminder template

WhatsApp only allows free-form messages within 24 hours of the patient's last message. A day-before reminder
is usually outside that window, so it needs an **approved message template**. Create one with four
variables, category **Utility**, in each language the clinic uses:

> Hello {{1}}, this is a reminder of your appointment on {{2}} with {{3}} (reference {{4}}). If you can't
> make it, please reply to this message or call us.

> Guten Tag {{1}}, wir erinnern Sie an Ihren Termin am {{2}} bei {{3}} (Buchungsnummer {{4}}). Falls Sie
> verhindert sind, antworten Sie bitte auf diese Nachricht oder rufen Sie uns an.

- **Meta:** create it under WhatsApp Manager → Message templates, with languages `en` and `de`. Put its name
  in `WHATSAPP_REMINDER_TEMPLATE`.
- **Twilio:** create it in Content Template Builder and submit it for WhatsApp approval. Put its `HX…` SID
  in `TWILIO_REMINDER_CONTENT_SID`.

Without a template, reminders to patients who haven't written in the last 24 hours fail. After three
attempts the failure shows in **Settings → Channels → Upcoming reminders**, and a callback task is added to
the handoff queue.

## How it behaves

- **One conversation per phone number.** The model's context continues across messages.
- **Text only.** Photos and voice notes get a polite "please type your question" reply.
- **Safety.** Booking still needs the patient's name, email and phone, and a "yes" in a separate message.
  The WhatsApp number alone is not treated as proof of identity.
- **Staff take-over.** Staff replies from the Conversations page are delivered to the patient's WhatsApp.
- **Retries and limits.** Provider retries are recognised, so the patient never gets a reply twice. One
  number can trigger at most 30 assistant replies an hour, which limits LLM cost from spam.
- **Security.** Every webhook is checked against the provider's signature (Twilio: `X-Twilio-Signature`;
  Meta: `X-Hub-Signature-256`). Requests that fail the check are refused and written to the audit log.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Audit log shows `webhook denied` (Twilio) | `PUBLIC_BASE_URL` doesn't match the URL in Twilio exactly (http vs https, trailing slash, other host). |
| Audit log shows `webhook denied` (Meta) | Wrong app secret, or the webhook belongs to another app. |
| Meta "Verify and save" fails | The verify token differs, or the server isn't reachable at that address. |
| "more than 24 hours have passed" | Expected for messages outside the window; set up the reminder template. |
| Test message not delivered | WhatsApp only delivers free-form messages to numbers that wrote to you in the last 24 hours (and, in the Twilio sandbox, to numbers that joined it). |
