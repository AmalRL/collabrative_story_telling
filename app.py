"""
Campaign Performance Dashboard — loads metrics from GCS and renders Plotly charts.
"""

import io
import json
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from google.cloud import storage
from google.oauth2 import service_account

# GCS source — Render: set GOOGLE_APPLICATION_CREDENTIALS_JSON to the SA JSON body
BUCKET_NAME = "btpss-dashboard-data"
CSV_BLOB_NAME = "campaign_metrics.csv"

COL_BROAD_TAG = "Broad Tag"
COL_CAMPAIGN_NO = "Campaign No."

# CSV may use "Campaign No" without a trailing period
_CAMPAIGN_NO_ALIASES = ("Campaign No", "Campaign No.", "Campaign Number")

PERIOD_LABELS = ("Pre", "Campaign", "Post")
PERIOD_COLORS = {
    "Pre": "gray",
    "Campaign": "blue",
    "Post": "green",
}

METRIC_CHARTS = [
    {
        "title": "Activation %",
        "pre": "Pre Activation %",
        "campaign": "Campaign Activation %",
        "post": "Post Activation %",
    },
    {
        "title": "RM Activation %",
        "pre": "Pre RM Activation %",
        "campaign": "Campaign RM Activation %",
        "post": "Post RM Activation %",
    },
    {
        "title": "DAU %",
        "pre": "Pre Avg DAU %",
        "campaign": "Campaign Avg DAU %",
        "post": "Post Avg DAU %",
    },
    {
        "title": "WAU %",
        "pre": "Pre WAU %",
        "campaign": "Campaign WAU %",
        "post": "Post WAU %",
    },
    {
        "title": "Average Messages per Group",
        "pre": "Pre Avg Messages / Group",
        "campaign": "Campaign Avg Messages / Group",
        "post": "Post Avg Messages / Group",
    },
]


def _storage_client() -> storage.Client:
    """
    Build a GCS client from Render-friendly env vars.

    Prefer GOOGLE_APPLICATION_CREDENTIALS_JSON (full service account JSON string).
    Otherwise use Application Default Credentials, including
    GOOGLE_APPLICATION_CREDENTIALS when set to a JSON file path.
    """
    json_body = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if json_body:
        info = json.loads(json_body)
        credentials = service_account.Credentials.from_service_account_info(info)
        project = info.get("project_id")
        return storage.Client(credentials=credentials, project=project)
    return storage.Client()


def _prepare_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Strip headers and map known column aliases to canonical names."""
    df = dataframe.copy()
    df.columns = df.columns.str.strip()

    if COL_CAMPAIGN_NO not in df.columns:
        for alias in _CAMPAIGN_NO_ALIASES:
            if alias in df.columns:
                df = df.rename(columns={alias: COL_CAMPAIGN_NO})
                break
        else:
            lower_to_actual = {col.lower(): col for col in df.columns}
            for alias in _CAMPAIGN_NO_ALIASES:
                match = lower_to_actual.get(alias.lower())
                if match:
                    df = df.rename(columns={match: COL_CAMPAIGN_NO})
                    break

    return df


def _campaign_label(value) -> str:
    """Display label for x-axis grouping."""
    if pd.isna(value):
        return "Unknown"
    if isinstance(value, float) and value.is_integer():
        return f"Campaign {int(value)}"
    return f"Campaign {value}"


@st.cache_data(ttl=600)
def load_campaign_data():
    """
    Load campaign_metrics.csv from GCS.

    Auth: GOOGLE_APPLICATION_CREDENTIALS_JSON on Render, or
    GOOGLE_APPLICATION_CREDENTIALS (file path) / ADC locally.
    Cache TTL (600s) re-fetches from the bucket when it expires.
    """
    client = _storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(CSV_BLOB_NAME)

    if not blob.exists():
        raise FileNotFoundError(
            f"'{CSV_BLOB_NAME}' was not found in bucket '{BUCKET_NAME}'."
        )

    raw_bytes = blob.download_as_bytes()
    dataframe = pd.read_csv(io.BytesIO(raw_bytes))
    return _prepare_dataframe(dataframe)


def melt_metric_wide_to_long(
    dataframe: pd.DataFrame,
    pre_column: str,
    campaign_column: str,
    post_column: str,
) -> pd.DataFrame:
    """Convert one wide metric (Pre / Campaign / Post columns) to long format."""
    id_columns = [
        c
        for c in dataframe.columns
        if c not in (pre_column, campaign_column, post_column)
    ]
    long_df = dataframe.melt(
        id_vars=id_columns,
        value_vars=[pre_column, campaign_column, post_column],
        var_name="_source_column",
        value_name="Metric value",
    )
    column_to_period = {
        pre_column: "Pre",
        campaign_column: "Campaign",
        post_column: "Post",
    }
    long_df["Period"] = long_df["_source_column"].map(column_to_period)
    long_df["Metric value"] = pd.to_numeric(long_df["Metric value"], errors="coerce")
    long_df["Campaign"] = long_df[COL_CAMPAIGN_NO].apply(_campaign_label)
    return long_df


def plot_metric(
    dataframe: pd.DataFrame,
    title: str,
    pre_column: str,
    campaign_column: str,
    post_column: str,
    *,
    show_legend: bool = True,
) -> go.Figure:
    """Build a grouped bar chart (Pre, Campaign, Post) per campaign."""
    long_df = melt_metric_wide_to_long(
        dataframe, pre_column, campaign_column, post_column
    )

    # Preserve campaign order from filtered data
    campaign_order = (
        long_df.drop_duplicates(subset=[COL_CAMPAIGN_NO])
        .sort_values(COL_CAMPAIGN_NO)["Campaign"]
        .tolist()
    )

    fig = go.Figure()
    for period in PERIOD_LABELS:
        period_df = long_df[long_df["Period"] == period]
        fig.add_trace(
            go.Bar(
                name=period,
                x=period_df["Campaign"],
                y=period_df["Metric value"],
                marker_color=PERIOD_COLORS[period],
                legendgroup=period,
                showlegend=show_legend,
                hovertemplate=(
                    "Campaign: %{customdata[0]}<br>"
                    "Period: %{customdata[1]}<br>"
                    "Broad Tag: %{customdata[2]}<br>"
                    "Metric value: %{y}<extra></extra>"
                ),
                customdata=period_df[
                    ["Campaign", "Period", COL_BROAD_TAG]
                ].values,
            )
        )

    fig.update_layout(
        title=title,
        barmode="group",
        xaxis_title="",
        yaxis_title=title,
        legend_title="Period",
        margin=dict(t=60, b=40),
        xaxis=dict(categoryorder="array", categoryarray=campaign_order),
    )
    return fig


def apply_sidebar_filters(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Apply Broad Tag and Campaign No. filters from the sidebar."""
    filtered = dataframe.copy()

    if COL_BROAD_TAG in filtered.columns:
        tags = sorted(filtered[COL_BROAD_TAG].dropna().astype(str).unique())
        selected_tag = st.sidebar.selectbox("Broad Tag", ["All"] + tags, index=0)
        if selected_tag != "All":
            filtered = filtered[
                filtered[COL_BROAD_TAG].astype(str) == selected_tag
            ]

    if COL_CAMPAIGN_NO in filtered.columns:
        campaigns = sorted(filtered[COL_CAMPAIGN_NO].dropna().unique())
        campaign_labels = ["All"] + [
            _campaign_label(c) for c in campaigns
        ]
        label_to_no = {
            _campaign_label(c): c for c in campaigns
        }
        selected_campaign_label = st.sidebar.selectbox(
            "Campaign", campaign_labels, index=0
        )
        if selected_campaign_label != "All":
            campaign_no = label_to_no[selected_campaign_label]
            filtered = filtered[filtered[COL_CAMPAIGN_NO] == campaign_no]

    return filtered


def main() -> None:
    st.set_page_config(
        page_title="Campaign Performance Dashboard",
        layout="wide",
    )
    st.title("Campaign Performance Dashboard")

    try:
        raw_df = load_campaign_data()
    except FileNotFoundError as exc:
        st.error(str(exc))
        return
    except Exception as exc:
        st.error(f"Unable to reach bucket or load data: {exc}")
        return

    if raw_df is None or raw_df.empty:
        st.warning("Campaign metrics data is empty.")
        return

    if COL_CAMPAIGN_NO not in raw_df.columns:
        st.error(
            f"Required column '{COL_CAMPAIGN_NO}' not found. "
            f"Columns in CSV: {', '.join(raw_df.columns.astype(str))}"
        )
        return

    filtered_df = apply_sidebar_filters(raw_df)
    if filtered_df.empty:
        st.warning("No rows match the selected filters.")
        return

    for index, chart_spec in enumerate(METRIC_CHARTS):
        fig = plot_metric(
            filtered_df,
            chart_spec["title"],
            chart_spec["pre"],
            chart_spec["campaign"],
            chart_spec["post"],
            show_legend=(index == 0),
        )
        st.plotly_chart(fig, use_container_width=True)


if __name__ == "__main__":
    main()
