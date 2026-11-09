"""Run with:  streamlit run app.py"""
import io
import os

import pandas as pd
import streamlit as st

from spotify_metadata import SpotifyClient, SpotifyError, extract

st.set_page_config(page_title="Spotify Metadata Extractor", layout="wide")
st.title("Spotify Metadata Extractor")

with st.sidebar:
    st.header("Spotify credentials")
    cid = st.text_input("Client ID", value=os.getenv("SPOTIFY_CLIENT_ID", ""))
    secret = st.text_input("Client Secret", value=os.getenv("SPOTIFY_CLIENT_SECRET", ""), type="password")
    credits = st.checkbox("Composition, lyrics, production & engineering credits (MusicBrainz, slow ~2-3 s/track)")
    st.caption("These credits are not in the Spotify API; MusicBrainz coverage is partial.")

links = st.text_area("Spotify URLs or URIs (one per line)", height=120,
                     placeholder="https://open.spotify.com/album/...\nspotify:track:...")

if st.button("Extract", type="primary"):
    lines = [l.strip() for l in links.splitlines() if l.strip()]
    if not lines:
        st.error("Paste at least one URL or URI.")
        st.stop()
    try:
        client = SpotifyClient(cid, secret)
    except SpotifyError as e:
        st.error(str(e))
        st.stop()

    frames, bar = [], st.progress(0.0)
    for n, link in enumerate(lines):
        try:
            df, warns = extract(client, link, credits,
                                progress=lambda p, n=n: bar.progress((n + p) / len(lines)))
            frames.append(df)
            for w in warns:
                st.warning(w)
        except SpotifyError as e:
            st.error(f"{link}: {e}")
    bar.empty()

    if frames:
        st.session_state["df"] = pd.concat(frames, ignore_index=True)

df = st.session_state.get("df")
if df is not None and not df.empty:
    st.success(f"{len(df)} tracks")
    st.dataframe(df, use_container_width=True)
    c1, c2 = st.columns(2)
    c1.download_button("Download CSV", df.to_csv(index=False, encoding="utf-8-sig"),
                       "spotify_metadata.csv", "text/csv")
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    c2.download_button("Download Excel", buf.getvalue(), "spotify_metadata.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
