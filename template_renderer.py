import html
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ViralTemplate:
    id: str
    label: str
    description: str
    accent: str
    secondary: str


VIRAL_TEMPLATES = [
    ViralTemplate(
        id="hook_burst",
        label="Hook Burst",
        description="Kinetic headline, creator-credit footer, and punchy color flashes.",
        accent="#f43f5e",
        secondary="#8b5cf6",
    ),
    ViralTemplate(
        id="cinematic_stack",
        label="Cinematic Stack",
        description="Premium film-frame treatment with stacked title bars and soft scanlines.",
        accent="#fbbf24",
        secondary="#14b8a6",
    ),
    ViralTemplate(
        id="trend_radar",
        label="Trend Radar",
        description="Viral-feed inspired overlays with signal meters and source badges.",
        accent="#38bdf8",
        secondary="#d946ef",
    ),
    ViralTemplate(
        id="documentary_interview",
        label="Documentary Interview",
        description="Interview-frame treatment for the viral 'if this had a documentary' format.",
        accent="#f8fafc",
        secondary="#ef4444",
    ),
    ViralTemplate(
        id="then_vs_now",
        label="Then vs. Now",
        description="Split-screen transformation frame for before/after and glow-up trends.",
        accent="#22c55e",
        secondary="#f97316",
    ),
    ViralTemplate(
        id="movie_star_entrance",
        label="Movie Star Entrance",
        description="Cinematic premiere reveal with spotlights, title glow, and red-carpet energy.",
        accent="#facc15",
        secondary="#dc2626",
    ),
    ViralTemplate(
        id="meme_master_reaction",
        label="Meme Master Reaction",
        description="Reaction-card wrapper with comment bubbles, punchline stickers, and meme pacing.",
        accent="#a3e635",
        secondary="#06b6d4",
    ),
    ViralTemplate(
        id="summer_schedule_loop",
        label="Summer Schedule Loop",
        description="Routine-loop overlay for day-in-the-life, seasonal rhythm, and workflow videos.",
        accent="#fb7185",
        secondary="#38bdf8",
    ),
]


def list_viral_templates():
    return [
        {
            "id": template.id,
            "label": template.label,
            "description": template.description,
        }
        for template in VIRAL_TEMPLATES
    ]


def get_viral_template(template_id: str) -> ViralTemplate:
    for template in VIRAL_TEMPLATES:
        if template.id == template_id:
            return template
    return VIRAL_TEMPLATES[0]


def _run(cmd, cwd: Optional[str] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

def _font_path() -> str:
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/opt/homebrew/share/fonts/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(path):
            return path
    return ""

def _ffmpeg_escape_text(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")

def _render_ffmpeg_template(source_video_path: str, output_path: str, template: ViralTemplate, title: str, subtitle: str, timeout: int):
    font = _font_path()
    title_safe = _ffmpeg_escape_text((title or template.label).upper()[:54])
    subtitle_safe = _ffmpeg_escape_text((subtitle or "6Frame Studio").upper()[:70])
    font_arg = f":fontfile='{font}'" if font else ""
    vf = ",".join([
        "scale=1080:1920:force_original_aspect_ratio=increase",
        "crop=1080:1920",
        "eq=saturation=1.12:contrast=1.08:brightness=-0.05",
        "drawbox=x=44:y=44:w=992:h=1832:color=white@0.22:t=3",
        f"drawbox=x=0:y=0:w=1080:h=1920:color=black@0.18:t=fill",
        f"drawtext=text='6FRAME STUDIO'{font_arg}:x=72:y=72:fontsize=34:fontcolor=white",
        f"drawtext=text='{_ffmpeg_escape_text(template.label.upper())}'{font_arg}:x=w-tw-72:y=74:fontsize=26:fontcolor=white:box=1:boxcolor=black@0.45:boxborderw=14",
        f"drawtext=text='{title_safe}'{font_arg}:x=72:y=h-360:fontsize=68:fontcolor=white:box=1:boxcolor=black@0.28:boxborderw=18",
        f"drawtext=text='{subtitle_safe}'{font_arg}:x=76:y=h-190:fontsize=34:fontcolor=white:box=1:boxcolor=black@0.22:boxborderw=12",
        "drawbox=x=76:y=h-92:w=928:h=12:color=white@0.16:t=fill",
        f"drawbox=x=76:y=h-92:w=705:h=12:color={template.accent.replace('#', '0x')}@0.95:t=fill",
    ])
    cmd = [
        "ffmpeg", "-y", "-i", source_video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-movflags", "+faststart",
        output_path,
    ]
    result = _run(cmd, timeout=timeout)
    if result.returncode != 0 and "No such filter: 'drawtext'" in (result.stderr or ""):
        vf_no_text = ",".join([
            "scale=1080:1920:force_original_aspect_ratio=increase",
            "crop=1080:1920",
            "eq=saturation=1.12:contrast=1.08:brightness=-0.05",
            "drawbox=x=0:y=0:w=1080:h=1920:color=black@0.18:t=fill",
            "drawbox=x=44:y=44:w=992:h=1832:color=white@0.22:t=3",
            f"drawbox=x=76:y=h-92:w=928:h=12:color=white@0.16:t=fill",
            f"drawbox=x=76:y=h-92:w=705:h=12:color={template.accent.replace('#', '0x')}@0.95:t=fill",
        ])
        cmd[5] = vf_no_text
        result = _run(cmd, timeout=timeout)
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"FFmpeg template fallback failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")


def _probe_duration(video_path: str) -> float:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return 8.0
    try:
        return max(1.0, min(float(result.stdout.strip()), 30.0))
    except ValueError:
        return 8.0


def _copy_source_video(source_path: str, assets_dir: str) -> str:
    _, ext = os.path.splitext(source_path)
    ext = ext if ext else ".mp4"
    dest_name = f"source{ext}"
    dest_path = os.path.join(assets_dir, dest_name)
    shutil.copy2(source_path, dest_path)
    return f"assets/{dest_name}"


def _template_variant_markup(template_id: str) -> str:
    if template_id == "documentary_interview":
        return """
        <div class="doc-rec">REC</div>
        <div class="doc-focus"></div>
        <div class="doc-lower"><span>CONFESSIONAL</span><strong>UNFILTERED STORY MODE</strong></div>
        """
    if template_id == "then_vs_now":
        return """
        <div class="split-line"></div>
        <div class="split-tag split-tag-left">THEN</div>
        <div class="split-tag split-tag-right">NOW</div>
        <div class="growth-card">TRANSFORMATION ARC</div>
        """
    if template_id == "movie_star_entrance":
        return """
        <div class="spotlight spotlight-left"></div>
        <div class="spotlight spotlight-right"></div>
        <div class="premiere-card">WORLD PREMIERE</div>
        <div class="marquee">MAIN CHARACTER MOMENT</div>
        """
    if template_id == "meme_master_reaction":
        return """
        <div class="comment-card comment-one">wait for it...</div>
        <div class="comment-card comment-two">NO WAY</div>
        <div class="reaction-stamp">REPLAY THIS</div>
        """
    if template_id == "summer_schedule_loop":
        return """
        <div class="loop-ring"></div>
        <div class="schedule-card schedule-one"><span>09:00</span><strong>HOOK</strong></div>
        <div class="schedule-card schedule-two"><span>13:00</span><strong>PAYOFF</strong></div>
        <div class="schedule-card schedule-three"><span>18:00</span><strong>LOOP</strong></div>
        """
    return """
        <div class="corner tl"></div>
        <div class="corner br"></div>
        """


def _template_html(
    template: ViralTemplate,
    source_rel_path: str,
    title: str,
    subtitle: str,
    duration: float,
) -> str:
    title_text = html.escape(title or "Viral AI Video System")
    subtitle_text = html.escape(subtitle or "6Frame Studio template render")
    title_words = title_text.split(" ", 1)
    if len(title_words) == 2:
        title_html = f"<strong>{title_words[0]}</strong> {title_words[1]}"
    else:
        title_html = f"<strong>{title_text}</strong>"
    duration_attr = f"{duration:.3f}"
    variables = json.dumps(
        [
            {"id": "title", "type": "string", "label": "Title", "default": title or "Viral AI Video System"},
            {"id": "subtitle", "type": "string", "label": "Subtitle", "default": subtitle or "6Frame Studio template render"},
        ]
    )
    variant_markup = _template_variant_markup(template.id)

    escaped_variables = html.escape(variables, quote=True)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1080, height=1920" />
    <title>6Frame Viral Template</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>
      * {{ box-sizing: border-box; }}
      html, body {{
        width: 1080px;
        height: 1920px;
        margin: 0;
        overflow: hidden;
        background: #050507;
        color: #f8fafc;
        font-family: Inter, Outfit, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      #root {{
        position: relative;
        width: 1080px;
        height: 1920px;
        overflow: hidden;
        background: #050507;
      }}
      .clip {{
        position: absolute;
        inset: 0;
      }}
      #source-video {{
        width: 1080px;
        height: 1920px;
        object-fit: cover;
        filter: saturate(1.12) contrast(1.08) brightness(0.78);
      }}
      #source-audio {{
        display: none;
      }}
      .shade {{
        background:
          radial-gradient(circle at 25% 12%, {template.secondary}66 0, transparent 30%),
          radial-gradient(circle at 82% 70%, {template.accent}55 0, transparent 26%),
          linear-gradient(180deg, rgba(0,0,0,0.55), rgba(0,0,0,0.02) 42%, rgba(0,0,0,0.82));
      }}
      .scan {{
        opacity: 0.24;
        background-image: linear-gradient(rgba(255,255,255,0.08) 1px, transparent 1px);
        background-size: 100% 9px;
        mix-blend-mode: soft-light;
      }}
      .frame {{
        position: absolute;
        inset: 44px;
        border: 2px solid rgba(255,255,255,0.24);
        box-shadow: inset 0 0 0 1px rgba(255,255,255,0.08), 0 0 80px rgba(0,0,0,0.45);
      }}
      .brand {{
        position: absolute;
        top: 72px;
        left: 72px;
        right: 72px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        font-size: 28px;
        font-weight: 800;
        letter-spacing: 0;
        text-transform: uppercase;
      }}
      .brand span {{
        color: {template.accent};
      }}
      .badge {{
        padding: 14px 20px;
        border: 1px solid rgba(255,255,255,0.24);
        border-radius: 999px;
        background: rgba(5,5,7,0.48);
        font-size: 22px;
        font-weight: 700;
        color: #e5e7eb;
      }}
      .headline {{
        position: absolute;
        left: 72px;
        right: 72px;
        bottom: 250px;
        font-size: 92px;
        line-height: 0.94;
        font-weight: 900;
        letter-spacing: 0;
        text-transform: uppercase;
        text-shadow: 0 18px 70px rgba(0,0,0,0.72);
      }}
      .headline strong {{
        color: {template.accent};
      }}
      .subtitle {{
        position: absolute;
        left: 76px;
        right: 76px;
        bottom: 142px;
        font-size: 34px;
        line-height: 1.18;
        color: #e5e7eb;
        text-shadow: 0 12px 40px rgba(0,0,0,0.72);
      }}
      .meter {{
        position: absolute;
        left: 76px;
        right: 76px;
        bottom: 82px;
        height: 12px;
        border-radius: 999px;
        background: rgba(255,255,255,0.14);
        overflow: hidden;
      }}
      .meter div {{
        width: 76%;
        height: 100%;
        background: linear-gradient(90deg, {template.accent}, {template.secondary});
        border-radius: inherit;
      }}
      .side-label {{
        position: absolute;
        right: 38px;
        top: 600px;
        writing-mode: vertical-rl;
        text-orientation: mixed;
        color: rgba(255,255,255,0.62);
        font-weight: 800;
        font-size: 24px;
        text-transform: uppercase;
      }}
      .corner {{
        position: absolute;
        width: 130px;
        height: 130px;
        border-color: {template.accent};
        border-style: solid;
        opacity: 0.9;
      }}
      .corner.tl {{ top: 44px; left: 44px; border-width: 5px 0 0 5px; }}
      .corner.br {{ right: 44px; bottom: 44px; border-width: 0 5px 5px 0; }}
      .doc-rec {{
        position: absolute;
        top: 146px;
        right: 86px;
        padding: 10px 16px;
        border: 1px solid rgba(255,255,255,0.34);
        border-radius: 999px;
        background: rgba(0,0,0,0.42);
        color: {template.secondary};
        font-size: 24px;
        font-weight: 900;
      }}
      .doc-rec::before {{
        content: "";
        display: inline-block;
        width: 12px;
        height: 12px;
        margin-right: 10px;
        border-radius: 999px;
        background: {template.secondary};
        box-shadow: 0 0 24px {template.secondary};
      }}
      .doc-focus {{
        position: absolute;
        left: 120px;
        right: 120px;
        top: 310px;
        height: 760px;
        border: 2px solid rgba(255,255,255,0.28);
        box-shadow: 0 0 0 999px rgba(0,0,0,0.16);
      }}
      .doc-lower {{
        position: absolute;
        left: 76px;
        right: 76px;
        bottom: 420px;
        padding: 22px 26px;
        border-left: 8px solid {template.secondary};
        background: rgba(0,0,0,0.64);
      }}
      .doc-lower span {{
        display: block;
        color: {template.secondary};
        font-size: 22px;
        font-weight: 900;
      }}
      .doc-lower strong {{
        display: block;
        margin-top: 6px;
        font-size: 34px;
      }}
      .split-line {{
        position: absolute;
        top: 210px;
        bottom: 210px;
        left: 50%;
        width: 4px;
        background: linear-gradient(180deg, transparent, {template.accent}, {template.secondary}, transparent);
        box-shadow: 0 0 30px {template.accent};
      }}
      .split-tag {{
        position: absolute;
        top: 230px;
        padding: 14px 20px;
        border-radius: 8px;
        background: rgba(0,0,0,0.64);
        font-size: 26px;
        font-weight: 900;
      }}
      .split-tag-left {{ left: 88px; color: {template.secondary}; }}
      .split-tag-right {{ right: 88px; color: {template.accent}; }}
      .growth-card {{
        position: absolute;
        left: 110px;
        right: 110px;
        top: 370px;
        padding: 18px;
        border: 1px solid rgba(255,255,255,0.18);
        background: rgba(0,0,0,0.42);
        text-align: center;
        font-size: 24px;
        font-weight: 900;
      }}
      .spotlight {{
        position: absolute;
        top: -120px;
        width: 420px;
        height: 1580px;
        background: linear-gradient(180deg, rgba(255,255,255,0.34), rgba(250,204,21,0.06), transparent 80%);
        filter: blur(10px);
        transform-origin: top center;
        mix-blend-mode: screen;
      }}
      .spotlight-left {{ left: 30px; transform: rotate(17deg); }}
      .spotlight-right {{ right: 30px; transform: rotate(-17deg); }}
      .premiere-card {{
        position: absolute;
        top: 172px;
        left: 50%;
        transform: translateX(-50%);
        padding: 14px 28px;
        border: 1px solid rgba(250,204,21,0.6);
        background: rgba(0,0,0,0.58);
        color: {template.accent};
        font-size: 24px;
        font-weight: 900;
      }}
      .marquee {{
        position: absolute;
        left: 76px;
        right: 76px;
        bottom: 412px;
        padding: 18px;
        border-top: 2px solid {template.accent};
        border-bottom: 2px solid {template.accent};
        background: rgba(127,29,29,0.48);
        color: #fff7ed;
        text-align: center;
        font-size: 28px;
        font-weight: 900;
      }}
      .comment-card {{
        position: absolute;
        max-width: 520px;
        padding: 22px 26px;
        border-radius: 18px;
        background: rgba(15,23,42,0.82);
        border: 1px solid rgba(255,255,255,0.18);
        box-shadow: 0 16px 60px rgba(0,0,0,0.38);
        font-size: 32px;
        font-weight: 900;
      }}
      .comment-one {{ top: 270px; left: 70px; color: #f8fafc; }}
      .comment-two {{ top: 430px; right: 70px; color: {template.accent}; }}
      .reaction-stamp {{
        position: absolute;
        right: 82px;
        bottom: 416px;
        padding: 18px 22px;
        border: 5px solid {template.accent};
        color: {template.accent};
        background: rgba(0,0,0,0.36);
        font-size: 34px;
        font-weight: 900;
        transform: rotate(-6deg);
      }}
      .loop-ring {{
        position: absolute;
        right: 86px;
        top: 246px;
        width: 164px;
        height: 164px;
        border: 16px solid rgba(255,255,255,0.16);
        border-top-color: {template.accent};
        border-right-color: {template.secondary};
        border-radius: 999px;
      }}
      .schedule-card {{
        position: absolute;
        left: 76px;
        width: 330px;
        padding: 16px 20px;
        border-radius: 14px;
        border: 1px solid rgba(255,255,255,0.2);
        background: rgba(2,6,23,0.74);
      }}
      .schedule-card span {{
        color: {template.secondary};
        font-size: 22px;
        font-weight: 900;
      }}
      .schedule-card strong {{
        display: block;
        margin-top: 4px;
        font-size: 30px;
      }}
      .schedule-one {{ top: 260px; }}
      .schedule-two {{ top: 380px; }}
      .schedule-three {{ top: 500px; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-width="1080" data-height="1920" data-duration="{duration_attr}" data-composition-variables='{escaped_variables}'>
      <video id="source-video" class="clip" src="{source_rel_path}" data-start="0" data-duration="{duration_attr}" data-track-index="0" muted playsinline></video>
      <audio id="source-audio" src="{source_rel_path}" data-start="0" data-duration="{duration_attr}" data-track-index="10" data-volume="1"></audio>
      <section id="overlays" class="clip" data-start="0" data-duration="{duration_attr}" data-track-index="1">
        <div class="shade"></div>
        <div class="scan"></div>
        <div class="frame"></div>
        {variant_markup}
        <div class="brand"><div><span>6</span>FRAME STUDIO</div><div class="badge">{html.escape(template.label)}</div></div>
        <div class="side-label">AI SOCIAL ENGINE</div>
        <div class="headline" id="headline">{title_html}</div>
        <div class="subtitle" id="subtitle">{subtitle_text}</div>
        <div class="meter"><div id="meter-fill"></div></div>
      </section>
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      const vars = window.__hyperframes && window.__hyperframes.getVariables ? window.__hyperframes.getVariables() : {{}};
      if (vars.title) document.getElementById("headline").textContent = vars.title;
      if (vars.subtitle) document.getElementById("subtitle").textContent = vars.subtitle;
      const tl = gsap.timeline({{ paused: true }});
      const has = selector => document.querySelector(selector);
      tl.from("#source-video", {{ scale: 1.09, duration: {duration_attr}, ease: "none" }}, 0);
      tl.from(".brand", {{ y: -48, opacity: 0, duration: 0.5, ease: "power3.out" }}, 0.15);
      tl.from(".headline", {{ y: 110, opacity: 0, duration: 0.72, ease: "power4.out" }}, 0.35);
      tl.from(".subtitle", {{ y: 48, opacity: 0, duration: 0.55, ease: "power3.out" }}, 0.72);
      if (has(".doc-rec,.split-tag,.premiere-card,.comment-card,.schedule-card")) tl.from(".doc-rec,.split-tag,.premiere-card,.comment-card,.schedule-card", {{ y: -34, opacity: 0, stagger: 0.08, duration: 0.48, ease: "power3.out" }}, 0.22);
      if (has(".doc-lower,.growth-card,.marquee,.reaction-stamp")) tl.from(".doc-lower,.growth-card,.marquee,.reaction-stamp", {{ y: 44, opacity: 0, duration: 0.55, ease: "power3.out" }}, 0.48);
      if (has(".split-line,.loop-ring")) tl.from(".split-line,.loop-ring", {{ opacity: 0, scaleY: 0.2, duration: 0.6, ease: "power3.out" }}, 0.18);
      tl.from("#meter-fill", {{ width: "0%", duration: {max(0.8, duration - 0.8):.3f}, ease: "none" }}, 0.35);
      tl.to(".scan", {{ y: 36, duration: {duration_attr}, ease: "none" }}, 0);
      if (has(".corner.tl")) tl.to(".corner.tl", {{ x: 16, y: 16, duration: {duration_attr}, ease: "sine.inOut" }}, 0);
      if (has(".corner.br")) tl.to(".corner.br", {{ x: -16, y: -16, duration: {duration_attr}, ease: "sine.inOut" }}, 0);
      if (has(".loop-ring")) tl.to(".loop-ring", {{ rotation: 360, duration: {duration_attr}, ease: "none" }}, 0);
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
"""


def render_template_video(
    *,
    source_video_path: str,
    output_path: str,
    template_id: str,
    title: str,
    subtitle: str,
    work_root: str,
    quality: str = "standard",
    timeout: int = 240,
) -> Dict[str, str]:
    if not os.path.exists(source_video_path):
        raise FileNotFoundError(f"Source video not found: {source_video_path}")

    template = get_viral_template(template_id)
    project_dir = os.path.join(work_root, f"template_render_{uuid.uuid4().hex}")
    assets_dir = os.path.join(project_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    source_rel_path = _copy_source_video(source_video_path, assets_dir)
    duration = _probe_duration(source_video_path)
    html_path = os.path.join(project_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_template_html(template, source_rel_path, title, subtitle, duration))

    render_quality = quality if quality in {"draft", "standard", "high"} else "standard"
    result = _run(
        [
            "npx",
            "-y",
            "hyperframes@0.7.40",
            "render",
            "--quality",
            render_quality,
            "--output",
            output_path,
        ],
        cwd=project_dir,
        timeout=timeout,
    )
    render_engine = "hyperframes"
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        try:
            _render_ffmpeg_template(source_video_path, output_path, template, title, subtitle, timeout)
            render_engine = "ffmpeg_fallback"
        except Exception as fallback_error:
            raise RuntimeError(
                "HyperFrames render failed and FFmpeg fallback failed.\n"
                f"hyperframes stdout:\n{result.stdout}\n"
                f"hyperframes stderr:\n{result.stderr}\n"
                f"fallback error:\n{fallback_error}"
            )

    return {
        "template_id": template.id,
        "template_label": template.label,
        "project_dir": project_dir,
        "output_path": output_path,
        "render_engine": render_engine,
    }
