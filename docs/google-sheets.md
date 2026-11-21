# Connecting Google Sheets

Staff edit doctors, services, weekly availability and exceptions (time off, extra hours) in a Google Sheet. The assistant pulls the sheet every
2 minutes (`SYNC_INTERVAL_SECONDS`), and immediately when someone presses **Sync** (`POST /api/admin/data/sync`).
Bookings are made in the assistant's local SQLite database, which makes double booking impossible. They are then
written back to the `patients` and `appointments` tabs within a few seconds. Bookable times are generated from the
`availability` and `exceptions` tabs. Hours you change in the admin calendar are written back to the sheet too.

## 1. Create the sheet

Template link: `GOOGLE_SHEETS_TEMPLATE_URL` (placeholder until the project publishes one).

Until then:
1. Download `templates/clinic_template.xlsx` from this repository.
2. In Google Drive, choose **New → File upload**, then open the file with **Google Sheets**. Use
   **File → Save as Google Sheets**.
3. Keep the tab names and column headers exactly as they are. The `README` tab explains every column.

## 2. Create a service account (one time, about 5 minutes)

1. Go to <https://console.cloud.google.com/> and create a project, for example "clinic-assistant".
2. Go to **APIs & Services → Library** and enable the **Google Sheets API**.
3. Go to **IAM & Admin → Service Accounts → Create service account**. Any name works, and it needs no roles.
4. Open the account. Under **Keys → Add key → Create new key → JSON**, download the file.
5. Save the file on the server, for example as `config/google-service-account.json`. Treat it like a password and never commit it.

## 3. Share the sheet with the service account

Open the sheet and choose **Share**. Paste the service account's email (it looks like
`name@project.iam.gserviceaccount.com`, and is also the `client_email` field in the JSON file) and give it **Editor** access.

## 4. Configure `.env`

```
DATA_SOURCE=google_sheets
GOOGLE_SHEETS_ID=1AbC...xyz          # from the URL: docs.google.com/spreadsheets/d/<id>/edit
GOOGLE_SERVICE_ACCOUNT_FILE=config/google-service-account.json
```

Restart the server. To load the demo clinic into the sheet (this overwrites every tab), run this from `backend/`:

```bash
..\.venv\Scripts\python -m scripts.seed_demo --store google_sheets
```

Check the connection with `GET /api/admin/data/status` (sends the `X-Admin-Token` header).

## Rules for editing

- **Dates and times** use the clinic's local time. The safest format is plain text: `2026-11-24 09:00`.
- **Weekly hours** go in the `availability` tab: one row per doctor, weekday and block, e.g. `1, Tuesday, 14:00, 17:00`.
- **Exceptions** go in the `exceptions` tab:
  - `off` for time off; leave `doctor_id` empty to close the whole clinic.
  - `extra` for one-off extra hours (needs a doctor).
  - A whole day runs from `00:00` to `00:00` the next day.
- **Removing a doctor or service:** delete its row or set `active` to `FALSE`. The doctor or service is hidden, and its history is kept.
- **Time off over an existing booking** doesn't delete the booking. The sync status names it so you can contact the patient.
- **App-owned tabs:** `appointments` and `patients` are written by the assistant. You may add a new patient (row without `id`); other edits are overwritten.
- **Problem rows:** a row with a problem, such as a bad date or an unknown doctor id, is skipped. The rest of the sheet still syncs, and the problem shows in the sync status and the server log.
- **Wrong tab headers:** if a tab's headers are wrong, that whole tab is ignored, so the data in the assistant stays as it was.
