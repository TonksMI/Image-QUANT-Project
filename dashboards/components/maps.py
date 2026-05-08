"""pydeck and plotly choropleth helpers."""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def _colormap(values: pd.Series, cmap: str = "YlOrRd", alpha: int = 180) -> list[list[int]]:
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors
    vals = values.fillna(0).values.astype(float)
    vmin, vmax = vals.min(), vals.max()
    if vmax == vmin:
        vmax = vmin + 1e-9
    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
    fn   = cm.get_cmap(cmap)
    return [[int(r * 255), int(g * 255), int(b * 255), alpha]
            for r, g, b, _ in fn(norm(vals))]


# ---------------------------------------------------------------------------
# pydeck H3 map
# ---------------------------------------------------------------------------

def h3_deck(
    df: pd.DataFrame,
    value_col: str,
    h3_col: str = "h3_index",
    cmap: str = "YlOrRd",
    center_lat: float = 33.45,
    center_lon: float = -112.07,
    zoom: int = 9,
    tooltip_extra: list[str] | None = None,
    alpha: int = 180,
):
    """Return a pydeck Deck with a single H3HexagonLayer.

    Returns None when pydeck is not installed or df is empty.
    """
    try:
        import pydeck as pdk
    except ImportError:
        return None

    if df.empty or h3_col not in df.columns or value_col not in df.columns:
        return None

    plot_df = df[[h3_col, value_col] + (tooltip_extra or [])].copy()
    plot_df["_color"] = _colormap(plot_df[value_col], cmap=cmap, alpha=alpha)

    extra_html = "".join(
        f"<br/>{c}: {{{c}}}" for c in (tooltip_extra or [])
    )
    tooltip = {
        "html": f"<b>{{{h3_col}}}</b><br/>{value_col}: {{{value_col}}}{extra_html}",
        "style": {"backgroundColor": "#1e1e1e", "color": "white",
                  "fontSize": "12px", "padding": "6px"},
    }

    layer = pdk.Layer(
        "H3HexagonLayer",
        data=plot_df,
        get_hexagon=h3_col,
        get_fill_color="_color",
        extruded=False,
        pickable=True,
        auto_highlight=True,
        opacity=alpha / 255,
    )

    view = pdk.ViewState(latitude=center_lat, longitude=center_lon,
                         zoom=zoom, pitch=0, bearing=0)
    return pdk.Deck(
        layers=[layer],
        initial_view_state=view,
        tooltip=tooltip,
        map_style="mapbox://styles/mapbox/dark-v10",
    )


def h3_deck_dual(
    df: pd.DataFrame,
    left_col: str,
    right_col: str,
    h3_col: str = "h3_index",
    center_lat: float = 33.45,
    center_lon: float = -112.07,
    zoom: int = 9,
):
    """Two pydeck Deck objects for side-by-side comparison. Returns (left, right)."""
    return (
        h3_deck(df, left_col,  h3_col=h3_col, center_lat=center_lat, center_lon=center_lon, zoom=zoom),
        h3_deck(df, right_col, h3_col=h3_col, cmap="Blues", center_lat=center_lat, center_lon=center_lon, zoom=zoom),
    )


# ---------------------------------------------------------------------------
# Plotly choropleth (county level)
# ---------------------------------------------------------------------------

def county_choropleth(
    df: pd.DataFrame,
    fips_col: str,
    value_col: str,
    title: str = "",
    color_scale: str = "YlOrRd",
):
    """Return a plotly choropleth figure for US counties.

    df must have a FIPS column (5-digit string, zero-padded) and a value column.
    Returns None if plotly is not installed or df is empty.
    """
    try:
        import plotly.express as px
    except ImportError:
        return None

    if df.empty:
        return None

    df = df.copy()
    df[fips_col] = df[fips_col].astype(str).str.zfill(5)

    fig = px.choropleth(
        df,
        geojson="https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json",
        locations=fips_col,
        color=value_col,
        scope="usa",
        color_continuous_scale=color_scale,
        title=title,
    )
    fig.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=420)
    return fig


def state_choropleth(
    df: pd.DataFrame,
    state_col: str,
    value_col: str,
    title: str = "",
    color_scale: str = "YlOrRd",
):
    """Plotly choropleth at state level (2-letter abbreviations)."""
    try:
        import plotly.express as px
    except ImportError:
        return None

    if df.empty:
        return None

    fig = px.choropleth(
        df,
        locations=state_col,
        locationmode="USA-states",
        color=value_col,
        scope="usa",
        color_continuous_scale=color_scale,
        title=title,
    )
    fig.update_layout(margin=dict(l=0, r=0, t=30, b=0), height=420)
    return fig
