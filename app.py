"""Clifton Blake Lead Uploader — CSV to Salesforce lead importer."""
import io
import csv
import requests
import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="CB Lead Uploader",
    page_icon="📤",
    layout="centered",
)

# ── Password gate ────────────────────────────────────────────────────────────
def _check_password():
    correct = st.secrets.get("app_password", "")
    if not correct:
        return  # no secret set — skip gate (dev mode)
    if st.session_state.get("authenticated"):
        return
    st.title("CB Lead Uploader")
    pwd = st.text_input("Password", type="password")
    if pwd and pwd == correct:
        st.session_state["authenticated"] = True
        st.rerun()
    elif pwd:
        st.error("Incorrect password.")
    st.stop()

_check_password()

# ── Mobile-friendly CSS tweaks ──────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stMetricValue"] { font-size: 1.4rem !important; }
[data-testid="stMetricLabel"] { font-size: 0.85rem !important; }
</style>
""", unsafe_allow_html=True)

# ── Salesforce Auth ─────────────────────────────────────────────────────────
def get_sf_credentials():
    """Pull Salesforce credentials from Streamlit secrets."""
    consumer_key = st.secrets.get("sf_consumer_key", "")
    consumer_secret = st.secrets.get("sf_consumer_secret", "")
    login_url = st.secrets.get("sf_login_url", "")
    if not all([consumer_key, consumer_secret, login_url]):
        return None, None, None
    return consumer_key, consumer_secret, login_url


def authenticate_salesforce(consumer_key, consumer_secret, login_url):
    """Get access token using OAuth Client Credentials flow."""
    response = requests.post(
        f"{login_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": consumer_key,
            "client_secret": consumer_secret,
        },
    )
    if response.status_code != 200:
        return None, None, response.text
    token_data = response.json()
    return token_data["access_token"], token_data["instance_url"], None


def check_existing_leads(access_token, instance_url, emails):
    """Query Salesforce for leads that already exist by email."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    # Build SOQL query with email list
    email_list = ", ".join(f"'{e}'" for e in emails if e)
    if not email_list:
        return set()
    query = f"SELECT Email FROM Lead WHERE Email IN ({email_list})"
    response = requests.get(
        f"{instance_url}/services/data/v62.0/query/",
        headers=headers,
        params={"q": query},
    )
    if response.status_code != 200:
        return set()
    records = response.json().get("records", [])
    return {r["Email"].lower() for r in records if r.get("Email")}


def create_lead(access_token, instance_url, lead_data):
    """Create a single lead in Salesforce."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        f"{instance_url}/services/data/v62.0/sobjects/Lead/",
        headers=headers,
        json=lead_data,
    )
    if response.status_code == 201:
        return True, response.json()["id"], None
    return False, None, response.text


# ── Name parsing ────────────────────────────────────────────────────────────
def parse_name(full_name):
    """Split full name into first and last. Salesforce requires LastName."""
    parts = full_name.strip().split()
    if len(parts) == 0:
        return "", "Unknown"
    elif len(parts) == 1:
        return "", parts[0]
    else:
        return parts[0], " ".join(parts[1:])


# ── Main App ────────────────────────────────────────────────────────────────
st.title("CB Lead Uploader")
st.caption("Upload a CSV of marketing leads to Salesforce")

# Check Salesforce credentials
consumer_key, consumer_secret, login_url = get_sf_credentials()
if not consumer_key:
    st.error(
        "Salesforce credentials not configured. "
        "Add `sf_consumer_key`, `sf_consumer_secret`, and `sf_login_url` to `.streamlit/secrets.toml`."
    )
    st.stop()

st.markdown("---")

# ── File Upload ─────────────────────────────────────────────────────────────
uploaded_file = st.file_uploader("Drop your leads CSV here", type=["csv"])

if uploaded_file is None:
    st.info("Upload a CSV file with columns: **Full Name**, **Email**, and optionally **Phone**, **Investment Objective**, **Ad Source**, **Date**, **Notes**.")
    st.stop()

# ── Parse CSV ───────────────────────────────────────────────────────────────
try:
    df = pd.read_csv(uploaded_file)
except Exception as e:
    st.error(f"Could not read CSV: {e}")
    st.stop()

# Validate required columns
required_cols = {"Full Name", "Email"}
missing = required_cols - set(df.columns)
if missing:
    st.error(f"CSV is missing required columns: **{', '.join(missing)}**")
    st.stop()

# Clean up
df = df.fillna("")
lead_count = len(df)

if lead_count == 0:
    st.warning("CSV is empty — no leads found.")
    st.stop()

# ── Preview ─────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("Preview")

c1, c2 = st.columns(2)
c1.metric("Total Leads", str(lead_count))
c2.metric("With Email", str(len(df[df["Email"].str.strip() != ""])))

# Flag potential issues
flagged = []
for i, row in df.iterrows():
    notes = []
    name = str(row["Full Name"]).strip()
    email = str(row["Email"]).strip()
    if len(name.split()) < 2:
        notes.append("Missing last name")
    if "@" not in email:
        notes.append("Invalid email")
    if notes:
        flagged.append({"Row": i + 1, "Name": name, "Email": email, "Issue": ", ".join(notes)})

if flagged:
    st.warning(f"{len(flagged)} lead(s) flagged with potential issues:")
    st.dataframe(pd.DataFrame(flagged), use_container_width=True, hide_index=True)

st.dataframe(df, use_container_width=True, hide_index=True)

# ── Duplicate Check ─────────────────────────────────────────────────────────
st.markdown("---")

# Check for duplicates on file upload
if "dup_checked_file" not in st.session_state:
    st.session_state["dup_checked_file"] = None
    st.session_state["existing_emails"] = set()
    st.session_state["exclude_set"] = set()

file_id = uploaded_file.name + str(uploaded_file.size)
if st.session_state["dup_checked_file"] != file_id:
    with st.spinner("Checking Salesforce for existing leads..."):
        access_token, instance_url, auth_error = authenticate_salesforce(
            consumer_key, consumer_secret, login_url
        )
        if auth_error:
            st.error(f"Authentication failed: {auth_error}")
            st.stop()
        emails = [str(row["Email"]).strip() for _, row in df.iterrows()]
        st.session_state["existing_emails"] = check_existing_leads(access_token, instance_url, emails)
        st.session_state["dup_checked_file"] = file_id
        st.session_state["exclude_set"] = set()

existing_emails = st.session_state["existing_emails"]

if existing_emails:
    dup_rows = df[df["Email"].str.strip().str.lower().isin(existing_emails)]
    st.warning(f"{len(dup_rows)} lead(s) already exist in Salesforce:")

    for i, row in dup_rows.iterrows():
        email = str(row["Email"]).strip()
        name = str(row["Full Name"]).strip()
        checked = st.checkbox(
            f"Include **{name}** ({email}) anyway",
            value=False,
            key=f"dup_{email}",
        )
        if checked:
            st.session_state["exclude_set"].discard(email.lower())
        else:
            st.session_state["exclude_set"].add(email.lower())

    new_count = lead_count - len(st.session_state["exclude_set"])
    st.caption(f"{new_count} lead(s) will be uploaded, {len(st.session_state['exclude_set'])} will be skipped.")
else:
    st.success("No duplicates found — all leads are new.")

# ── Lead Source + Company ───────────────────────────────────────────────────
st.markdown("---")
st.subheader("Salesforce Settings")

col_s, col_c = st.columns(2)
with col_s:
    lead_source = st.text_input("Lead Source", value="Instagram Ad")
with col_c:
    company = st.text_input("Company", value="CB Marketing Lead")

# ── Upload Button ───────────────────────────────────────────────────────────
st.markdown("---")

if "upload_results" not in st.session_state:
    st.session_state["upload_results"] = None

exclude_set = st.session_state.get("exclude_set", set())
upload_count = lead_count - len(exclude_set)

if st.button(f"Upload {upload_count} Leads to Salesforce", type="primary", use_container_width=True):
    # Authenticate
    with st.spinner("Authenticating with Salesforce..."):
        access_token, instance_url, auth_error = authenticate_salesforce(
            consumer_key, consumer_secret, login_url
        )
    if auth_error:
        st.error(f"Authentication failed: {auth_error}")
        st.stop()

    st.success("Authenticated successfully.")

    # Upload leads
    results = []
    progress = st.progress(0, text="Uploading leads...")

    for i, row in df.iterrows():
        first_name, last_name = parse_name(str(row["Full Name"]))
        email = str(row["Email"]).strip()

        # Skip excluded duplicates
        if email.lower() in exclude_set:
            results.append({
                "Name": f"{first_name} {last_name}".strip(),
                "Email": email,
                "Status": "Skipped (duplicate)",
                "Salesforce ID": "",
                "Error": "",
            })
            progress.progress((i + 1) / lead_count, text=f"Processing {i + 1}/{lead_count}...")
            continue

        # Build description from optional columns
        desc_parts = []
        if row.get("Investment Objective", ""):
            desc_parts.append(f"Investment Objective: {row['Investment Objective']}")
        if row.get("Ad Source", ""):
            desc_parts.append(f"Ad Source: {row['Ad Source']}")
        if row.get("Date", ""):
            desc_parts.append(f"Date: {row['Date']}")
        if row.get("Notes", ""):
            desc_parts.append(f"Notes: {row['Notes']}")

        lead_data = {
            "FirstName": first_name,
            "LastName": last_name,
            "Email": email,
            "Phone": str(row.get("Phone", "")).strip() or None,
            "Company": company,
            "LeadSource": lead_source,
        }
        if desc_parts:
            lead_data["Description"] = "\n".join(desc_parts)

        success, lead_id, error = create_lead(access_token, instance_url, lead_data)
        results.append({
            "Name": f"{first_name} {last_name}".strip(),
            "Email": lead_data["Email"],
            "Status": "Created" if success else "Failed",
            "Salesforce ID": lead_id or "",
            "Error": error or "",
        })

        progress.progress((i + 1) / lead_count, text=f"Uploading {i + 1}/{lead_count}...")

    progress.empty()
    st.session_state["upload_results"] = results

# ── Results ─────────────────────────────────────────────────────────────────
if st.session_state["upload_results"]:
    results = st.session_state["upload_results"]
    results_df = pd.DataFrame(results)

    created = len(results_df[results_df["Status"] == "Created"])
    skipped = len(results_df[results_df["Status"] == "Skipped (duplicate)"])
    failed = len(results_df[results_df["Status"] == "Failed"])

    st.markdown("---")
    st.subheader("Results")

    rc1, rc2, rc3 = st.columns(3)
    rc1.metric("Created", str(created))
    rc2.metric("Skipped", str(skipped))
    rc3.metric("Failed", str(failed))

    def color_status(row):
        if row["Status"] == "Created":
            return ["background-color: #d4edda; color: #155724"] * len(row)
        if row["Status"] == "Skipped (duplicate)":
            return ["background-color: #fff3cd; color: #856404"] * len(row)
        return ["background-color: #f8d7da; color: #721c24"] * len(row)

    display_df = results_df[["Name", "Email", "Status", "Salesforce ID"]].copy()
    st.dataframe(
        display_df.style.apply(color_status, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    if failed > 0:
        with st.expander("View Errors"):
            error_df = results_df[results_df["Status"] == "Failed"][["Name", "Email", "Error"]]
            st.dataframe(error_df, use_container_width=True, hide_index=True)
