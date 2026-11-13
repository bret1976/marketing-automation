# Footage mode (`VIDEO_ENGINE=footage`): a 31–55 second Short with no generation API

## Length rule (2026-11-12)

Every footage clip, both the 9:16 and the 16:9 file, is between **31 and 55 seconds**, measured with ffprobe on the output. It aims for about 45 s (`FOOTAGE_TARGET_SECONDS`, clamped to 31–55). The length comes from the voiceover and the footage that is actually available. It never comes from the app's `autonomous_video_duration` setting, so an old 8-second setting can't shrink a footage clip. If there isn't enough usable on-topic footage to reach 31 s, that topic fails and the next topic is tried; the clip is never padded with black or shipped short. After rendering, `produce_footage` re-checks both files and raises if either one is outside 31–55 s. The voiceover is read at one natural speed (`VO_SPEED` 0.95, 0.6 s between lines) and is never sped up; if it would run past the clip, the last lines are dropped instead.

This mode is off by default. When `VIDEO_ENGINE=footage` is set, `growth_v2.produce()` builds the slot's video from real, free, legally reusable archival footage. It does not call Gemini Omni, Veo, Runway or FAL. Everything after `produce()` is unchanged: captions, title dedupe, the approval queue, scheduling, the x-safe-v1 X split, PostProxy publishing and metrics. Cost is $0 per video.

## Pipeline

| Step | How it works (no paid API) | Code |
|---|---|---|
| Topic (viral first) | The last-24h viral scan picks the subject. `todays_viral` runs the keyless finder (`open_viral.find_viral`, yt-dlp, uploads from today sorted by views) over a broad query set (`BROAD_VIRAL_QUERIES`: #shorts, viral, trending, documentary, nature, wildlife, space, disasters, aviation and more; override with `FOOTAGE_VIRAL_QUERIES`). It probes the most-viewed ~80 (`FOOTAGE_VIRAL_PROBE_LIMIT`) for views per hour. `viral_topics` then works down that list. It skips live streams, gaming, music, film/TV, reactions, news and politics. Each remaining title is resolved to the real-world subject it names (a Wikipedia article whose words all appear in the title). It also skips living people, brands and media, countries and cities, and abstract concepts. Only titles and view counts are read: **the viral videos are never downloaded or recut**. | `todays_viral`, `viral_topics`, `wiki_subjects`, `subject_ok` |
| Script and concept | When the viral subject is one of the 12 hand-checked library topics (`footage_topics.py`), its fact-checked script is used. Otherwise the script comes from the subject's Wikipedia lead: whole sentences up to about 105 words, years spoken out, read as one VO line per sentence, with captions stepping through caption-sized pieces. The post caption credits "Narration adapted from Wikipedia (CC BY-SA 4.0)" with the article link. No LLM. If there's too little text for 31 s, the subject is skipped. | `topic_from_subject`, `script_from_text`, `build_concept` |
| Footage for a scanned subject | If the Wikipedia article has a Wikidata Commons category (P373), clips and stills are taken only from that category (`incategory:` = curated, plus `deepcat:` hits whose title names the subject). A plain name search pulls in homonyms such as trails, silent films and cartoons. Without a category, the search is title-only and uses homonym/film/sports/politics excludes. At most 2 shots come from one source clip. Only PD/CC0/CC BY/CC BY-SA files are used. | `_wiki_page`, `topic_from_subject`, `gather_candidates`, `plan_shots` |
| Fallback | None. A subject without enough licensed on-topic footage for 31 s is skipped and the next viral subject is tried (`FOOTAGE_TOPIC_ATTEMPTS`, default 4). If no viral subject works, the slot fails and nothing is posted. A canned library topic is **never** used just to fill the slot. | `produce_footage` |
| Footage sources | NASA Image & Video Library API. Wikimedia Commons API, keeping only public domain, CC0 and CC BY (SA, NC and ND are rejected). Internet Archive, limited to curated public-domain collections: Prelinger, Universal Newsreels and FedFlix. Licenses asserted by Internet Archive uploaders are not trusted. Pexels and Pixabay are used only if `PEXELS_API_KEY` / `PIXABAY_API_KEY` are set; they are free keys but this path is UNVERIFIED. Titles that are off-topic, talking heads, ceremonies, games or CGI are filtered out. | `search_*`, `gather_candidates` |
| Shot picking | Detects black bars baked into the frame and crops them. Scores candidate segments on exposure, tonal range, contrast, motion, sharpness and flat areas, and rejects segments with a cut inside the shot. Picks the best segments overall, at most 2 per source. Library topics can pin hand-checked moments, which are refined within ±3 s. | `detect_crop`, `segment_score`, `choose_segments`, `pinned_segments`, `plan_shots` |
| Edit | About 4–6 shots (2.2–11 s each; `shot_count`) that add up exactly to the planned 31–55 s, cut on the music beat (`shot_durations`). Pinned moments come first, then more gated segments from the same film and other sources. Each shot gets denoise and sharpen, then a desaturated cinematic grade: teal shadows, warm highlights, a light vignette and film grain. Ken Burns is applied only to stills. Ends with a 0.35 s fade. | `plan_total`, `fit_total`, `shot_durations`, `render` |
| Music | 4 CC0 Freesound excerpts, 56 s each, so a 55 s clip never loops or runs out of music (a short bed would be looped on a bar boundary). Their beat grids were analysed beforehand with librosa. Licenses are in `footage_assets/music/LICENSES.md`. | `footage_assets/music/` |
| Voiceover | Kokoro-82M through `kokoro-onnx`, run locally with ONNX (Apache-2.0). The free int8 model (88 MB) and the voices file (27 MB) download once into `STATE_DIR/kokoro`. The voice is am_michael 0.6 blended with am_onyx 0.4. If TTS fails, the video still ships with captions and music. | `synth_vo` |
| Captions | Burned in: the hook title for 0–2.8 s, voiceover captions timed on a soft plate, and a small source credit line. | `render` |
| Framing (9:16) | Full-bleed: every vertical frame is real picture edge to edge, with no blurred background or letterbox bars. Video shots are pan-and-scan: scaled to 108% of 1920 high, a 1080-wide window is centred on the shot's action centre (`focus_x`: edge plus motion energy, nudged toward the middle), then it pans slowly (10% of width over the shot, alternating direction per shot). Stills get a Ken Burns punch-in around the same centre. The hook sits at y=210 and captions/credit at y=1250/1420, clear of the top ~10% and bottom ~25% that platform UI covers. Captions use `fit_caption`: one line when it fits, otherwise a slightly smaller font, otherwise two balanced lines with no orphan word. On 16:9 the caption sits about 250 px above the credit line. Output is exactly the planned length in frames (for example 1350 frames for 45 s). | `_shot_filter`, `focus_x`, `fit_caption`, `render` |
| Output | `g2_footage_<id>.mp4` is 1080x1920 for Shorts, Reels, TikTok and Facebook. `..._16x9.mp4` is 1920x1080 for LinkedIn and X. Both are H.264 Main 4.0, yuv420p, 30 fps, AAC 44.1 kHz stereo, loudness-normalised, with `+faststart`. A sidecar `.sources.json` records each clip's page, author, license, file URL, segment, crop and whether Ken Burns was used. | `produce_footage` |
| Publishing | `post["landscape_video_path"]` is set. PostProxy sends the 16:9 file in LinkedIn/X batches and the 9:16 file everywhere else. `media_source` is `archival_footage`, so no made-with-AI or synthetic-media flags are set. | `main.publish_via_postproxy`, `growth_v2.build_post` |

## Audit: features that depend on Gemini or another API

| Feature | Depends on | Covered in footage mode? |
|---|---|---|
| Live trend scanner (`run_live_trend_scanner`, grounded search) | Gemini | **Yes, replaced** by the keyless open finder |
| Trend copy adaptation | Gemini | **Yes, replaced** by template copy |
| Concept planning (`plan_concept`) | Gemini | **Yes, replaced** by template concepts |
| 8 s video generation (Omni, Veo, Runway, FAL, Seedance) | Paid generation APIs | **Yes, replaced** by archival footage and the local edit |
| Caption-vs-frames vision check (`ensure_caption_matches`) | Gemini | **Partly.** There's no generated content to verify, so footage is chosen by metadata relevance plus pinned moments. Burned-in graphics in the source are not detected. |
| Transform/repost fallback (`build_transformed_clip`) | Gemini | No. It was already off by default. |
| Stitched 20–30 s Shorts (`stitched_shorts`) | Gemini and Veo/Seedance | No. Footage mode wins if both are set. |
| Virality score | Gemini | No (dashboard helper only) |
| Engagement replies (`draft_engagement_reply`) | Gemini | No |
| Repurpose a video link (`repurpose_video_link_copy`) | Gemini | No |
| Multi-agent pipeline / analysing an uploaded video | Gemini | No |
| Campaign planner, live listening, competitors | Gemini | No |
| Instagram hashtag research | Meta Graph (free) | Not used; topic hashtags are fixed |
| News context | Google News RSS (free) | Not needed |
| HyperFrames viral templates | Local Chromium | Unchanged |
| Captions, title dedupe, scheduling, approval, X policy | Local | **Yes, unchanged** |
| PostProxy and YouTube publishing, metrics | Posting APIs | **Yes, unchanged**, plus 16:9 routing for LinkedIn/X |

## Limits

- Topics come from whatever is going viral. Many days are dominated by gaming, music and live streams, which are skipped, so the usable pool can be small. If none of today's viral subjects has enough licensed footage, the slot posts nothing.
- Wikipedia-scripted subjects aren't hand fact-checked. They read the article lead, which is CC BY-SA 4.0, so it's credited in the post caption.
- Relevance for scanned subjects is a regex on the subject name, so a common-word subject (for example "Wolf") can still pull an occasional homonym clip.
- Automatic shot picking is based on metrics, so it can't see burned-in broadcast graphics. Pinned moments are used for D-Day and Artemis I. A follow-up could add local OCR (rapidocr-onnxruntime).
- Much archival footage is SD (480p) and gets upscaled. It looks like film, not 4K.
- Wikimedia returns HTTP 429 when requests arrive in bursts, so source probing runs sequentially with backoff and retries.
- The National Archives catalog API v2 needs a key. NARA films still arrive through Commons and the Internet Archive (Universal Newsreels, FedFlix).
- `kokoro-onnx` installs and runs on Python 3.11, and the int8 auto-download was tested on the box. The Railway container build is UNVERIFIED.
- Only PostProxy routes the 16:9 file. The direct (non-PostProxy) LinkedIn/X publishers still use `video_path`, which is the 9:16 file.

## Flags

- `VIDEO_ENGINE=footage` turns the mode on.
- `FOOTAGE_VIRAL_QUERIES` (separated by `|`) sets the finder's queries.
- `FOOTAGE_VOICE` sets the voice blend.
- `FOOTAGE_MAX_SOURCES` defaults to 6.
- `FOOTAGE_X264_PRESET` sets the x264 preset.
- `KOKORO_MODEL` and `KOKORO_VOICES` point to local model files.
- `PEXELS_API_KEY` and `PIXABAY_API_KEY` are optional free keys.

## Port plan: bret1976/autopilot-mcp (not built yet)

1. Vendor `footage_mode.py`, `footage_topics.py`, `footage_assets/music/` and `open_viral.py` into `app/`, wrapped with `asyncio.to_thread`.
2. In `app/originals.py`, add `ORIGINALS_ENGINE=footage`, set per buyer record or by env. `produce_original()` then calls `footage_mode.produce_footage()` instead of the `plan_concept` → `generate_veo_clip` → `ensure_caption_matches` chain. No buyer Gemini key is needed, and the cost cap is $0.
3. Change the MCP tools:
   - `scan_trends` returns open-finder results ranked against the library.
   - `write_copy` returns the template copy.
   - `run_autopilot` uses the buyer's engine.
4. `copy_for_original()` sets `landscape_video_path`. The PostProxy publisher sends 16:9 to LinkedIn/X and drops the AI flags for `archival_footage`.
5. Per-buyer niches: buyers outside history/space/engineering need a niche topic pack, or their own free Pexels/Pixabay key.
6. Add `kokoro-onnx` to requirements and store the model in `/data/kokoro`.
7. Port the tests, and add a test that `run_autopilot` in footage mode makes no Gemini call.

- 9:16 crops of low-resolution sources (for example the 622x480 Titanic reel) are upscaled about 3x and look soft. Prefer HD sources for vertical-first topics.
