// Javascript Frontend Logic for 6Frame Studio Multi-Agent Social Hub

// Escapes text pulled from external APIs (platform error responses, etc.) before it's
// interpolated into an innerHTML template — a failed publish call can return a raw HTML
// error page (e.g. Facebook's), and an unescaped <style>/<script> tag from that response
// would execute page-wide once inserted into the DOM.
function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function shortDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
}

document.addEventListener("DOMContentLoaded", () => {
    // State variables
    let uploadedVideoPath = "";
    let activeJobId = null;
    let pollInterval = null;

    // Elements
    const dropZone = document.getElementById("drop-zone");
    const fileInput = document.getElementById("file-input");
    const fileDetails = document.getElementById("file-details");
    const fileNameText = document.getElementById("file-name");
    const removeFileBtn = document.getElementById("remove-file-btn");
    const urlInput = document.getElementById("url-input");
    const triggerBtn = document.getElementById("trigger-btn");
    
    // Progress Elements
    const progressCard = document.getElementById("progress-card");
    const progressBar = document.getElementById("progress-bar");
    const statusMsg = document.getElementById("status-msg");
    const stepUpload = document.getElementById("step-upload");
    const stepContext = document.getElementById("step-context");
    const stepCopy = document.getElementById("step-copy");
    const stepReview = document.getElementById("step-review");

    // Review & Workspace Elements
    const reviewPlaceholder = document.getElementById("review-placeholder");
    const workspaceContent = document.getElementById("workspace-content");
    const linkedinText = document.getElementById("linkedin-text");
    const instagramText = document.getElementById("instagram-text");
    const tweetCardsContainer = document.getElementById("tweet-cards-container");
    const briefSummary = document.getElementById("brief-summary");
    const briefThemes = document.getElementById("brief-themes");
    const briefAlignment = document.getElementById("brief-alignment");
    const briefStyle = document.getElementById("brief-style");

    // Publishing Buttons
    const publishLinkedinBtn = document.getElementById("publish-linkedin-btn");
    const publishTwitterBtn = document.getElementById("publish-twitter-btn");
    const copyTwitterBtn = document.getElementById("copy-twitter-btn");

    // Settings Modal Elements
    const settingsBtn = document.getElementById("settings-btn");
    const settingsModal = document.getElementById("settings-modal");
    const closeSettingsBtn = document.getElementById("close-settings-btn");
    const saveSettingsBtn = document.getElementById("save-settings-btn");
    
    // Settings inputs
    const mockModeCheck = document.getElementById("mock-mode");
    const geminiKeyInput = document.getElementById("gemini-key");
    const runwayKeyInput = document.getElementById("runway-key");
    const falKeyInput = document.getElementById("fal-key");
    const brandVoiceInput = document.getElementById("brand-voice-input");
    const twConsumerKey = document.getElementById("tw-consumer-key");
    const twConsumerSecret = document.getElementById("tw-consumer-secret");
    const twAccessToken = document.getElementById("tw-access-token");
    const twAccessSecret = document.getElementById("tw-access-secret");
    const liAccessToken = document.getElementById("li-access-token");
    const liPersonUrn = document.getElementById("li-person-urn");

    // Toast
    const toast = document.getElementById("toast");
    const authOverlay = document.getElementById("auth-overlay");
    const authForm = document.getElementById("auth-form");
    const authPassword = document.getElementById("auth-password");
    const authError = document.getElementById("auth-error");

    // Load configurations initially
    checkAuth();

    /* ==========================================================================
       SETTINGS & MODAL
       ========================================================================== */
    
    settingsBtn.addEventListener("click", () => {
        fetchSettings();
        settingsModal.classList.remove("hidden");
    });

    closeSettingsBtn.addEventListener("click", () => {
        settingsModal.classList.add("hidden");
    });

    window.addEventListener("click", (e) => {
        if (e.target === settingsModal) {
            settingsModal.classList.add("hidden");
        }
    });

    function showToast(message, isError = false) {
        toast.textContent = message;
        toast.style.borderColor = isError ? "var(--error-color)" : "var(--success-color)";
        toast.classList.remove("hidden");
        setTimeout(() => {
            toast.classList.add("hidden");
        }, 3000);
    }

    function showAuth(message = "") {
        if (!authOverlay) return;
        authOverlay.classList.remove("hidden");
        if (authError) authError.textContent = message;
        if (authPassword) authPassword.focus();
    }

    function hideAuth() {
        if (authOverlay) authOverlay.classList.add("hidden");
        if (authPassword) authPassword.value = "";
        if (authError) authError.textContent = "";
    }

    function checkAuth() {
        fetch("/api/auth/status")
            .then(res => res.json())
            .then(data => {
                if (data.auth_required && !data.authenticated) {
                    showAuth();
                    return;
                }
                hideAuth();
                fetchSettings();
            })
            .catch(() => showAuth("Could not check admin session."));
    }

    if (authForm) {
        authForm.addEventListener("submit", (e) => {
            e.preventDefault();
            const btn = authForm.querySelector("button");
            if (btn) btn.disabled = true;
            fetch("/api/auth/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ password: authPassword.value })
            })
            .then(res => {
                if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Invalid password") });
                return res.json();
            })
            .then(() => {
                hideAuth();
                fetchSettings();
                fetchGrowthOS();
            })
            .catch(err => showAuth(err.message))
            .finally(() => {
                if (btn) btn.disabled = false;
            });
        });
    }

    const SOCIAL_BADGE_IDS = {
        twitter: "diag-twitter-status",
        linkedin: "diag-linkedin-status",
        instagram: "diag-instagram-status",
        tiktok: "diag-tiktok-status",
        youtube: "diag-youtube-status",
        facebook: "diag-facebook-status",
        threads: "diag-threads-status"
    };

    function paintBadge(el, label, tone) {
        if (!el) return;
        el.textContent = label;
        if (tone === "live") {
            el.style.background = "rgba(40, 167, 69, 0.2)";
            el.style.color = "#28a745";
        } else if (tone === "check") {
            el.style.background = "rgba(255, 193, 7, 0.18)";
            el.style.color = "#ffc107";
        } else {
            el.style.background = "rgba(220, 53, 69, 0.2)";
            el.style.color = "#dc3545";
        }
    }

    function updateDiagnosticBadges(data, socials) {
        paintBadge(document.getElementById("diag-gemini-status"), data.gemini_api_key ? "ACTIVE" : "INACTIVE", data.gemini_api_key ? "live" : "off");
        paintBadge(document.getElementById("diag-runway-status"), data.runway_api_key ? "ACTIVE" : "INACTIVE", data.runway_api_key ? "live" : "off");
        paintBadge(document.getElementById("diag-fal-status"), data.fal_api_key ? "ACTIVE" : "INACTIVE", data.fal_api_key ? "live" : "off");
        const native = {
            twitter: !!(data.twitter_consumer_key && data.twitter_consumer_secret && data.twitter_access_token && data.twitter_access_token_secret),
            linkedin: !!data.linkedin_access_token,
            instagram: !!(data.instagram_access_token && data.instagram_business_account_id),
            tiktok: !!data.tiktok_access_token,
            youtube: !!(data.youtube_client_id && data.youtube_client_secret && data.youtube_refresh_token),
            facebook: !!(data.facebook_page_access_token && data.facebook_page_id),
            threads: !!(data.threads_access_token && data.threads_user_id)
        };
        const byPlatform = {};
        (socials || []).forEach(item => { if (item && item.platform) byPlatform[item.platform] = item; });
        Object.keys(SOCIAL_BADGE_IDS).forEach(platform => {
            const channel = byPlatform[platform];
            const live = !!(channel && (channel.live || channel.status === "LIVE" || (channel.profile_status === "active")));
            if (live) {
                paintBadge(document.getElementById(SOCIAL_BADGE_IDS[platform]), "LIVE", "live");
            } else if (channel && channel.connected) {
                const label = channel.status === "EXPIRED" ? "EXPIRED" : "INACTIVE";
                paintBadge(document.getElementById(SOCIAL_BADGE_IDS[platform]), label, label === "EXPIRED" ? "check" : "off");
            } else {
                paintBadge(document.getElementById(SOCIAL_BADGE_IDS[platform]), native[platform] ? "CHECK" : "INACTIVE", native[platform] ? "check" : "off");
            }
        });
        const emailReady = !!(data.report_email_to && ((data.report_email_provider === "resend" && data.resend_api_key && data.resend_from) || (data.report_email_provider !== "resend" && data.smtp_host)));
        paintBadge(document.getElementById("diag-report-email-status"), emailReady ? "ACTIVE" : "INACTIVE", emailReady ? "live" : "off");
    }

    function setDiagnosticBadge(id, status) {
        const el = document.getElementById(id);
        if (!el) return;
        const ready = status === "ready";
        el.textContent = ready ? "LIVE" : "CHECK";
        el.style.background = ready ? "rgba(40, 167, 69, 0.2)" : "rgba(255, 193, 7, 0.18)";
        el.style.color = ready ? "#28a745" : "#ffc107";
    }

    function renderProviderDiagnostics(data) {
        const diagnostics = data.diagnostics || {};
        setDiagnosticBadge("diag-report-email-status", diagnostics.report_email?.status);
        const socials = diagnostics.postproxy?.socials || [];
        socials.forEach(channel => {
            const badgeId = SOCIAL_BADGE_IDS[channel.platform];
            if (!badgeId) return;
            if (channel.connected) {
                paintBadge(document.getElementById(badgeId), channel.status === "EXPIRED" ? "EXPIRED" : "LIVE", channel.status === "EXPIRED" ? "check" : "live");
            }
        });

        const output = document.getElementById("provider-diagnostics-output");
        if (!output) return;
        const rows = [
            ["Report Email", diagnostics.report_email],
            ["X Mentions", diagnostics.twitter_mentions],
            ["YouTube Comments", diagnostics.youtube_comments],
            ["PostProxy", diagnostics.postproxy]
        ];
        output.innerHTML = rows.map(([label, item]) => {
            const status = item?.status || "unknown";
            const message = item?.message || "";
            const extras = [
                item?.http_status ? `HTTP ${item.http_status}` : "",
                Number.isFinite(item?.sample_count) ? `${item.sample_count} samples` : "",
                item?.channel_title ? `Channel: ${item.channel_title}` : "",
                item?.has_force_ssl_scope === true ? "force-ssl scope" : "",
                item?.has_smtp_auth === true ? "SMTP auth set" : "",
                item?.has_resend_key === true ? "Resend key set" : "",
                item?.provider ? `Provider: ${item.provider}` : "",
                item?.profile_group_id ? `Group: ${item.profile_group_id}` : "",
                Number.isFinite(item?.profiles_count) ? `${item.profiles_count} profiles` : "",
                Array.isArray(item?.platforms) ? item.platforms.join(", ") : ""
            ].filter(Boolean).join(" · ");
            return `<div class="growth-list-item"><strong>${label} · ${status}</strong><span>${escapeHtml(message)}</span>${extras ? `<p>${escapeHtml(extras)}</p>` : ""}</div>`;
        }).join("");
    }

    function renderPostProxyProfiles(data) {
        const output = document.getElementById("postproxy-profiles-output");
        const socials = data.socials || [];
        const profiles = data.profiles || [];
        const groupId = data.profile_group_id || "";
        const groupInput = document.getElementById("postproxy-profile-group-id");
        if (groupInput && !groupInput.value && groupId) groupInput.value = groupId;
        if (output) {
            if (!profiles.length) {
                output.innerHTML = `<div class="growth-list-item"><strong>No connected profiles</strong><span>Use Connect on a platform to authorize through PostProxy.</span></div>`;
            } else {
                output.innerHTML = profiles.map(profile => {
                    const placement = (data.placements || {})[profile.platform] || {};
                    const placementLabel = placement.placement_id
                        ? `${placement.param || "placement"} ${placement.placement_id}${placement.placement_name ? " · " + placement.placement_name : ""}`
                        : "no placement id";
                    return `
                    <div class="growth-list-item">
                        <strong>${escapeHtml(profile.platform)} · ${escapeHtml(profile.name || "")}</strong>
                        <span>${escapeHtml(profile.status || "")} · ${escapeHtml(placementLabel)} · ${escapeHtml(profile.post_count || 0)} posts</span>
                        ${profile.expires_at ? `<p>Expires ${escapeHtml(shortDate(profile.expires_at))}</p>` : ""}
                    </div>`;
                }).join("");
            }
        }
        renderCockpitSocials(data);
        updateDiagnosticBadges(window.__lastSettings || {}, socials);
    }

    const DEFAULT_POSTPROXY_SOCIALS = [
        {platform:"instagram", label:"Instagram", status:"INACTIVE", live:false, connected:false},
        {platform:"facebook", label:"Facebook", status:"INACTIVE", live:false, connected:false},
        {platform:"tiktok", label:"TikTok", status:"INACTIVE", live:false, connected:false},
        {platform:"linkedin", label:"LinkedIn", status:"INACTIVE", live:false, connected:false},
        {platform:"twitter", label:"Twitter / X", status:"INACTIVE", live:false, connected:false},
        {platform:"youtube", label:"YouTube", status:"INACTIVE", live:false, connected:false},
        {platform:"threads", label:"Threads", status:"INACTIVE", live:false, connected:false}
    ];

    function renderCockpitSocials(data) {
        const grid = document.getElementById("cockpit-postproxy-socials");
        const status = document.getElementById("cockpit-postproxy-status");
        if (!grid) return;
        const socials = (data.socials && data.socials.length) ? data.socials : DEFAULT_POSTPROXY_SOCIALS;
        const pp = data.postproxy || data;
        if (status) {
            if (pp.key_valid === false || data.error || pp.error) {
                status.textContent = pp.error || data.error || "PostProxy key was rejected or no profiles could be synced.";
                status.dataset.tone = "error";
            } else if (pp.configured === false) {
                status.textContent = "PostProxy API key is not configured on this service.";
                status.dataset.tone = "error";
            } else {
                const count = socials.filter(item => item.connected).length;
                status.textContent = `${count} PostProxy profile${count === 1 ? "" : "s"} synced${pp.synced_at ? " · " + shortDate(pp.synced_at) : ""}.`;
                status.dataset.tone = "ok";
            }
        }
        grid.innerHTML = socials.map(channel => {
            const connected = !!channel.connected;
            const action = connected ? "Reconnect" : "Connect";
            const extra = [
                channel.profile_name,
                channel.placement_id ? `${channel.placement_param || "placement"} ${channel.placement_id}` : "",
                channel.expires_at ? `expires ${shortDate(channel.expires_at)}` : ""
            ].filter(Boolean).join(" · ");
            return `
                <div class="socials-row">
                    <div>
                        <strong>${escapeHtml(channel.label || channel.platform)}</strong>
                        <span>${escapeHtml(extra || "Not connected in PostProxy")}</span>
                    </div>
                    <div class="socials-row-actions">
                        <span class="socials-pill socials-pill-${escapeHtml((channel.status || "INACTIVE").toLowerCase())}">${escapeHtml(channel.status || "INACTIVE")}</span>
                        <button type="button" class="secondary-btn postproxy-connect-btn" data-platform="${escapeHtml(channel.platform)}" data-reconnect="${connected ? "true" : "false"}">${action}</button>
                    </div>
                </div>`;
        }).join("");
        grid.querySelectorAll(".postproxy-connect-btn").forEach(btn => {
            btn.addEventListener("click", () => connectPostProxyPlatform(btn.dataset.platform, btn.dataset.reconnect === "true"));
        });
    }

    function setPostProxyBusy(busy, label) {
        ["postproxy-refresh-profiles-btn", "cockpit-postproxy-sync-btn"].forEach(id => {
            const btn = document.getElementById(id);
            if (!btn) return;
            btn.disabled = busy;
            if (label) btn.textContent = label;
            else btn.textContent = id === "cockpit-postproxy-sync-btn" ? "Refresh / Sync" : "Refresh / Sync PostProxy";
        });
    }

    function refreshPostProxyProfiles() {
        setPostProxyBusy(true, "Syncing...");
        return fetch("/api/postproxy/sync", { method: "POST" })
            .then(res => {
                if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "PostProxy profile refresh failed") });
                return res.json();
            })
            .then(data => {
                renderPostProxyProfiles(data);
                return data;
            })
            .catch(err => {
                renderPostProxyProfiles({
                    socials: DEFAULT_POSTPROXY_SOCIALS,
                    profiles: [],
                    error: err.message,
                    configured: false
                });
                showToast(err.message, true);
            })
            .finally(() => setPostProxyBusy(false));
    }

    function connectPostProxyPlatform(platform, reconnect = false) {
        fetch("/api/postproxy/connect", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                platform,
                reconnect,
                profile_group_id: document.getElementById("postproxy-profile-group-id")?.value || "",
                redirect_url: window.location.origin + "/api/postproxy/callback"
            })
        })
        .then(res => {
            if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Could not start PostProxy connection") });
            return res.json();
        })
        .then(data => {
            if (data.url) {
                window.open(data.url, "_blank", "noopener,noreferrer");
                showToast(`PostProxy ${reconnect ? "reconnect" : "connect"} opened for ${platform}.`);
            } else if (data.already_connected && data.dashboard_url) {
                window.open(data.dashboard_url, "_blank", "noopener,noreferrer");
                showToast(`PostProxy ${platform} is already connected. Opened the PostProxy dashboard.`);
                refreshPostProxyProfiles();
            } else if (data.already_connected) {
                showToast(`PostProxy ${platform} is already connected.`);
                refreshPostProxyProfiles();
            } else {
                showToast("PostProxy did not return a connection URL.", true);
            }
        })
        .catch(err => showToast(err.message, true));
    }

    function fetchProviderDiagnostics() {
        const btn = document.getElementById("provider-diagnostics-btn");
        if (btn) {
            btn.disabled = true;
            btn.textContent = "Checking...";
        }
        return fetch("/api/provider-diagnostics")
            .then(res => {
                if (!res.ok) throw new Error("Provider diagnostics failed.");
                return res.json();
            })
            .then(data => {
                renderProviderDiagnostics(data);
                return data;
            })
            .catch(err => {
                showToast(err.message || "Provider diagnostics failed.", true);
            })
            .finally(() => {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = "Run Live Provider Diagnostics";
                }
            });
    }

    function fetchSettings() {
        fetch("/api/settings")
            .then(res => {
                if (res.status === 401) {
                    showAuth();
                    throw new Error("Authentication required.");
                }
                return res.json();
            })
            .then(data => {
                const setChecked = (id, value) => {
                    const el = document.getElementById(id);
                    if (el) el.checked = !!value;
                };
                const setValue = (id, value) => {
                    const el = document.getElementById(id);
                    if (el) el.value = value;
                };
                // Modal inputs
                mockModeCheck.checked = data.mock_mode;
                geminiKeyInput.value = data.gemini_api_key;
                runwayKeyInput.value = data.runway_api_key || "";
                if (falKeyInput) falKeyInput.value = data.fal_api_key || "";
                brandVoiceInput.value = data.brand_voice;
                twConsumerKey.value = data.twitter_consumer_key;
                twConsumerSecret.value = data.twitter_consumer_secret;
                twAccessToken.value = data.twitter_access_token;
                twAccessSecret.value = data.twitter_access_token_secret;
                liAccessToken.value = data.linkedin_access_token;
                liPersonUrn.value = data.linkedin_person_urn;
                
                // Autonomous settings
                const platforms = data.autonomous_platforms || [];
                document.getElementById("autonomous-posting").checked = data.autonomous_posting || false;
                document.getElementById("autonomous-hour").value = data.autonomous_hour || 9;
                document.getElementById("auto-platform-twitter").checked = platforms.includes("twitter");
                document.getElementById("auto-platform-linkedin").checked = platforms.includes("linkedin");
                document.getElementById("auto-platform-instagram").checked = platforms.includes("instagram");
                document.getElementById("auto-platform-tiktok").checked = platforms.includes("tiktok");
                document.getElementById("auto-platform-youtube").checked = platforms.includes("youtube");
                document.getElementById("auto-platform-facebook").checked = platforms.includes("facebook");
                document.getElementById("auto-platform-threads").checked = platforms.includes("threads");
                document.getElementById("autonomous-video-engine").value = data.autonomous_video_engine || "fal_hailuo_23";
                document.getElementById("autonomous-video-duration").value = data.autonomous_video_duration || 10;
                document.getElementById("require-autopilot-approval").checked = data.require_autopilot_approval !== false;
                setChecked("viral-template-enabled", data.viral_template_enabled || false);
                setValue("viral-template-style", data.viral_template_style || "hook_burst");
                setValue("viral-template-quality", data.viral_template_quality || "standard");

                // Additional platform credentials (modal)
                document.getElementById("public-base-url").value = data.public_base_url || "";
                document.getElementById("meta-app-id").value = data.meta_app_id || "";
                document.getElementById("meta-app-secret").value = data.meta_app_secret || "";
                document.getElementById("instagram-access-token").value = data.instagram_access_token || "";
                document.getElementById("instagram-business-account-id").value = data.instagram_business_account_id || "";
                document.getElementById("instagram-search-hashtags").value = (data.instagram_search_hashtags || []).join(", ");
                document.getElementById("facebook-page-access-token").value = data.facebook_page_access_token || "";
                document.getElementById("facebook-page-id").value = data.facebook_page_id || "";
                document.getElementById("tiktok-client-key").value = data.tiktok_client_key || "";
                document.getElementById("tiktok-client-secret").value = data.tiktok_client_secret || "";
                document.getElementById("tiktok-access-token").value = data.tiktok_access_token || "";
                document.getElementById("tiktok-refresh-token").value = data.tiktok_refresh_token || "";
                document.getElementById("youtube-client-id").value = data.youtube_client_id || "";
                document.getElementById("youtube-client-secret").value = data.youtube_client_secret || "";
                document.getElementById("youtube-refresh-token").value = data.youtube_refresh_token || "";
                document.getElementById("threads-access-token").value = data.threads_access_token || "";
                document.getElementById("threads-user-id").value = data.threads_user_id || "";
                setValue("report-email-provider", data.report_email_provider || "resend");
                setValue("report-email-to", data.report_email_to || "");
                setValue("resend-api-key", data.resend_api_key || "");
                setValue("resend-from", data.resend_from || "");
                setChecked("postproxy-enabled", data.postproxy_enabled || false);
                setValue("postproxy-api-key", data.postproxy_api_key || "");
                setValue("postproxy-profile-group-id", data.postproxy_profile_group_id || "");
                setValue("postproxy-daily-publish-limit", data.postproxy_daily_publish_limit || 2);
                setValue("smtp-host", data.smtp_host || "");
                setValue("smtp-port", data.smtp_port || 587);
                setValue("smtp-user", data.smtp_user || "");
                setValue("smtp-password", data.smtp_password || "");
                setValue("smtp-from", data.smtp_from || "");
                setChecked("smtp-tls", data.smtp_tls !== false);

                // Cockpit inputs
                document.getElementById("cockpit-mock-mode").checked = data.mock_mode;
                document.getElementById("cockpit-gemini-key").value = data.gemini_api_key || "";
                document.getElementById("cockpit-runway-key").value = data.runway_api_key || "";
                document.getElementById("cockpit-fal-key").value = data.fal_api_key || "";
                document.getElementById("cockpit-brand-voice").value = data.brand_voice || "";

                document.getElementById("cockpit-autonomous-posting").checked = data.autonomous_posting || false;
                document.getElementById("cockpit-autonomous-hour").value = data.autonomous_hour || 9;
                document.getElementById("cockpit-auto-platform-twitter").checked = platforms.includes("twitter");
                document.getElementById("cockpit-auto-platform-linkedin").checked = platforms.includes("linkedin");
                document.getElementById("cockpit-auto-platform-instagram").checked = platforms.includes("instagram");
                document.getElementById("cockpit-auto-platform-tiktok").checked = platforms.includes("tiktok");
                document.getElementById("cockpit-auto-platform-youtube").checked = platforms.includes("youtube");
                document.getElementById("cockpit-auto-platform-facebook").checked = platforms.includes("facebook");
                document.getElementById("cockpit-auto-platform-threads").checked = platforms.includes("threads");
                document.getElementById("cockpit-autonomous-video-engine").value = data.autonomous_video_engine || "fal_hailuo_23";
                document.getElementById("cockpit-autonomous-video-duration").value = data.autonomous_video_duration || 10;
                document.getElementById("cockpit-require-approval").checked = data.require_autopilot_approval !== false;
                setChecked("cockpit-viral-template-enabled", data.viral_template_enabled || false);
                setValue("cockpit-viral-template-style", data.viral_template_style || "hook_burst");
                setValue("cockpit-viral-template-quality", data.viral_template_quality || "standard");
                setValue("cockpit-postproxy-daily-publish-limit", data.postproxy_daily_publish_limit || 2);

                // Wizard toggle
                document.getElementById("wizard-autonomous-posting").checked = data.autonomous_posting || false;

                // Update health diagnostics
                window.__lastSettings = data;
                updateDiagnosticBadges(data);
                fetchProviderDiagnostics();
                refreshPostProxyProfiles();
            })
            .catch(err => {
                console.error("Failed to load settings:", err);
                showToast("Could not load settings.", true);
            });
    }

    // Shared across both save handlers (modal + cockpit)
    function collectAutoPlatforms(prefix) {
        const names = ["twitter", "linkedin", "instagram", "tiktok", "youtube", "facebook", "threads"];
        return names.filter(name => {
            const el = document.getElementById(`${prefix}-${name}`);
            return el && el.checked;
        });
    }

    function collectAdditionalPlatformCreds() {
        return {
            public_base_url: document.getElementById("public-base-url").value,
            meta_app_id: document.getElementById("meta-app-id").value,
            meta_app_secret: document.getElementById("meta-app-secret").value,
            instagram_access_token: document.getElementById("instagram-access-token").value,
            instagram_business_account_id: document.getElementById("instagram-business-account-id").value,
            instagram_search_hashtags: document.getElementById("instagram-search-hashtags").value
                .split(",").map(h => h.trim().replace(/^#/, "")).filter(h => h),
            facebook_page_access_token: document.getElementById("facebook-page-access-token").value,
            facebook_page_id: document.getElementById("facebook-page-id").value,
            tiktok_client_key: document.getElementById("tiktok-client-key").value,
            tiktok_client_secret: document.getElementById("tiktok-client-secret").value,
            tiktok_access_token: document.getElementById("tiktok-access-token").value,
            tiktok_refresh_token: document.getElementById("tiktok-refresh-token").value,
            youtube_client_id: document.getElementById("youtube-client-id").value,
            youtube_client_secret: document.getElementById("youtube-client-secret").value,
            youtube_refresh_token: document.getElementById("youtube-refresh-token").value,
            threads_access_token: document.getElementById("threads-access-token").value,
            threads_user_id: document.getElementById("threads-user-id").value,
            report_email_provider: document.getElementById("report-email-provider").value,
            report_email_to: document.getElementById("report-email-to").value,
            resend_api_key: document.getElementById("resend-api-key").value,
            resend_from: document.getElementById("resend-from").value,
            postproxy_enabled: document.getElementById("postproxy-enabled").checked,
            postproxy_api_key: document.getElementById("postproxy-api-key").value,
            postproxy_profile_group_id: document.getElementById("postproxy-profile-group-id").value,
            postproxy_daily_publish_limit: Math.max(1, parseInt(document.getElementById("postproxy-daily-publish-limit").value || "2", 10)),
            smtp_host: document.getElementById("smtp-host").value,
            smtp_port: parseInt(document.getElementById("smtp-port").value || "587", 10),
            smtp_user: document.getElementById("smtp-user").value,
            smtp_password: document.getElementById("smtp-password").value,
            smtp_from: document.getElementById("smtp-from").value,
            smtp_tls: document.getElementById("smtp-tls").checked
        };
    }

    const providerDiagnosticsBtn = document.getElementById("provider-diagnostics-btn");
    if (providerDiagnosticsBtn) {
        providerDiagnosticsBtn.addEventListener("click", fetchProviderDiagnostics);
    }
    const postProxyRefreshBtn = document.getElementById("postproxy-refresh-profiles-btn");
    if (postProxyRefreshBtn) {
        postProxyRefreshBtn.addEventListener("click", refreshPostProxyProfiles);
    }
    const cockpitSyncBtn = document.getElementById("cockpit-postproxy-sync-btn");
    if (cockpitSyncBtn) {
        cockpitSyncBtn.addEventListener("click", refreshPostProxyProfiles);
    }
    document.querySelectorAll(".postproxy-connect-btn").forEach(btn => {
        btn.addEventListener("click", () => connectPostProxyPlatform(btn.dataset.platform, btn.dataset.reconnect === "true"));
    });
    const postproxyReturn = new URLSearchParams(window.location.search).get("postproxy");
    if (["ok", "connected"].includes(postproxyReturn)) {
        showToast("PostProxy account connected. Syncing profiles...");
        refreshPostProxyProfiles();
    } else if (["failure", "failed"].includes(postproxyReturn)) {
        showToast("PostProxy connection was cancelled or failed.", true);
    }

    saveSettingsBtn.addEventListener("click", () => {
        const autoPlatforms = collectAutoPlatforms("auto-platform");

        const payload = {
            gemini_api_key: geminiKeyInput.value,
            runway_api_key: runwayKeyInput.value,
            fal_api_key: falKeyInput ? falKeyInput.value : "",
            brand_voice: brandVoiceInput.value,
            twitter_consumer_key: twConsumerKey.value,
            twitter_consumer_secret: twConsumerSecret.value,
            twitter_access_token: twAccessToken.value,
            twitter_access_token_secret: twAccessSecret.value,
            linkedin_access_token: liAccessToken.value,
            linkedin_person_urn: liPersonUrn.value,
            mock_mode: mockModeCheck.checked,
            autonomous_posting: document.getElementById("autonomous-posting").checked,
            autonomous_hour: parseInt(document.getElementById("autonomous-hour").value, 10),
            autonomous_platforms: autoPlatforms,
            autonomous_video_engine: document.getElementById("autonomous-video-engine").value,
            autonomous_video_duration: parseInt(document.getElementById("autonomous-video-duration").value, 10),
            require_autopilot_approval: document.getElementById("require-autopilot-approval").checked,
            viral_template_enabled: document.getElementById("viral-template-enabled").checked,
            viral_template_style: document.getElementById("viral-template-style").value,
            viral_template_quality: document.getElementById("viral-template-quality").value,
            ...collectAdditionalPlatformCreds()
        };

        fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
        .then(res => {
            if (res.ok) {
                showToast("Settings saved successfully!");
                settingsModal.classList.add("hidden");
            } else {
                throw new Error("Failed to save settings");
            }
        })
        .catch(err => {
            showToast("Failed to save settings.", true);
        });
    });

    /* ==========================================================================
       FILE UPLOAD & DRAG/DROP
       ========================================================================== */

    dropZone.addEventListener("click", () => fileInput.click());

    dropZone.addEventListener("dragover", (e) => {
        e.preventDefault();
        dropZone.classList.add("dragover");
    });

    dropZone.addEventListener("dragleave", () => {
        dropZone.classList.remove("dragover");
    });

    dropZone.addEventListener("drop", (e) => {
        e.preventDefault();
        dropZone.classList.remove("dragover");
        if (e.dataTransfer.files.length > 0) {
            handleFile(e.dataTransfer.files[0]);
        }
    });

    fileInput.addEventListener("change", (e) => {
        if (e.target.files.length > 0) {
            handleFile(e.target.files[0]);
        }
    });

    function handleFile(file) {
        if (file.type !== "video/mp4") {
            showToast("Please upload an MP4 video file.", true);
            return;
        }

        // Show mock upload or trigger backend upload
        fileNameText.textContent = file.name;
        fileDetails.classList.remove("hidden");
        dropZone.classList.add("hidden");
        
        // Disable trigger btn until upload finishes
        triggerBtn.disabled = true;
        triggerBtn.innerHTML = `<span>Uploading...</span>`;

        const formData = new FormData();
        formData.append("file", file);

        fetch("/api/upload", {
            method: "POST",
            body: formData
        })
        .then(res => {
            if (!res.ok) throw new Error("Upload failed");
            return res.json();
        })
        .then(data => {
            uploadedVideoPath = data.video_path;
            showToast("Video uploaded successfully.");
            triggerBtn.disabled = false;
            triggerBtn.innerHTML = `<span>Generate Social Campaigns</span>`;
        })
        .catch(err => {
            showToast("Video upload failed. Try again.", true);
            removeFile();
        });
    }

    removeFileBtn.addEventListener("click", removeFile);

    function removeFile() {
        uploadedVideoPath = "";
        fileInput.value = "";
        fileDetails.classList.add("hidden");
        dropZone.classList.remove("hidden");
        triggerBtn.disabled = true;
        triggerBtn.innerHTML = `<span>Generate Social Campaigns</span>`;
    }

    /* ==========================================================================
       PIPELINE TRIGGER & POLLING
       ========================================================================== */

    triggerBtn.addEventListener("click", () => {
        if (!uploadedVideoPath) return;

        const payload = {
            video_path: uploadedVideoPath,
            website_url: urlInput.value
        };

        // Reset Stepper
        resetStepper();
        progressCard.classList.remove("hidden");
        triggerBtn.disabled = true;
        removeFileBtn.disabled = true;
        urlInput.disabled = true;
        
        updateStepperUI(15, "Submitting job...");

        fetch("/api/analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
        .then(res => {
            if (!res.ok) throw new Error("Could not queue job");
            return res.json();
        })
        .then(data => {
            activeJobId = data.job_id;
            // Start polling
            pollInterval = setInterval(pollJobStatus, 3000);
        })
        .catch(err => {
            showToast("Failed to trigger agent orchestrator.", true);
            resetInputs();
        });
    });

    function resetInputs() {
        triggerBtn.disabled = false;
        removeFileBtn.disabled = false;
        urlInput.disabled = false;
        progressCard.classList.add("hidden");
    }

    function resetStepper() {
        progressBar.style.width = "0%";
        statusMsg.textContent = "Connecting to pipeline orchestrator...";
        [stepUpload, stepContext, stepCopy, stepReview].forEach(step => {
            step.className = "step";
        });
    }

    function updateStepperUI(progress, message) {
        progressBar.style.width = `${progress}%`;
        statusMsg.textContent = message;

        // Stepper Highlights
        if (progress >= 10 && progress <= 30) {
            stepUpload.classList.add("active");
        } else if (progress > 30 && progress <= 60) {
            stepUpload.classList.remove("active");
            stepUpload.classList.add("completed");
            stepContext.classList.add("active");
        } else if (progress > 60 && progress <= 90) {
            stepContext.classList.remove("active");
            stepContext.classList.add("completed");
            stepCopy.classList.add("active");
        } else if (progress > 90) {
            stepCopy.classList.remove("active");
            stepCopy.classList.add("completed");
            stepReview.classList.add("active");
        }
    }

    function pollJobStatus() {
        if (!activeJobId) return;

        fetch(`/api/status/${activeJobId}`)
            .then(res => res.json())
            .then(data => {
                if (data.status === "PROCESSING" || data.status === "PENDING") {
                    updateStepperUI(data.progress, data.message);
                } else if (data.status === "SUCCESS") {
                    clearInterval(pollInterval);
                    updateStepperUI(100, "Success!");
                    stepReview.classList.remove("active");
                    stepReview.classList.add("completed");
                    
                    showToast("Pipeline executed successfully!");
                    setTimeout(() => {
                        progressCard.classList.add("hidden");
                        populateWorkspace(data.result);
                        resetInputs();
                    }, 1500);
                } else if (data.status === "FAILED") {
                    clearInterval(pollInterval);
                    showToast(data.message, true);
                    resetInputs();
                }
            })
            .catch(err => {
                console.error("Polling error:", err);
            });
    }

    /* ==========================================================================
       WORKSPACE POPULATION
       ========================================================================== */

    function populateWorkspace(result) {
        reviewPlaceholder.classList.add("hidden");
        workspaceContent.classList.remove("hidden");

        // Set textareas
        linkedinText.value = result.copy.linkedin_post;
        instagramText.value = result.copy.instagram_caption;

        // Populate brief
        briefSummary.textContent = result.brief.video_summary;
        briefAlignment.textContent = result.brief.brand_alignment;
        
        briefThemes.innerHTML = "";
        result.brief.key_themes.forEach(theme => {
            const span = document.createElement("span");
            span.className = "tag";
            span.textContent = theme;
            briefThemes.appendChild(span);
        });

        briefStyle.innerHTML = "";
        result.brief.visual_style_tags.forEach(style => {
            const span = document.createElement("span");
            span.className = "tag";
            span.textContent = style;
            briefStyle.appendChild(span);
        });

        // Populate Twitter Thread cards
        tweetCardsContainer.innerHTML = "";
        const tweets = result.copy.twitter_thread || [result.copy.twitter_post];
        
        tweets.forEach((tweetText, index) => {
            const card = document.createElement("div");
            card.className = "tweet-card";
            card.innerHTML = `
                <div class="tweet-header">
                    <span>Tweet ${index + 1}</span>
                    <span class="badge">Draft</span>
                </div>
                <textarea class="tweet-textarea" rows="4">${tweetText}</textarea>
                <div class="counter-container">
                    <span class="char-count">${tweetText.length}</span>/280
                </div>
            `;

            const textarea = card.querySelector(".tweet-textarea");
            const charCountSpan = card.querySelector(".char-count");
            const counterContainer = card.querySelector(".counter-container");

            textarea.addEventListener("input", () => {
                const len = textarea.value.length;
                charCountSpan.textContent = len;
                if (len > 280) {
                    counterContainer.classList.add("danger");
                } else {
                    counterContainer.classList.remove("danger");
                }
            });

            tweetCardsContainer.appendChild(card);
        });
    }

    /* ==========================================================================
       TABS NAVIGATION
       ========================================================================== */

    const tabs = document.querySelectorAll(".tab-btn");
    const contents = document.querySelectorAll(".tab-content");

    tabs.forEach(tab => {
        tab.addEventListener("click", () => {
            tabs.forEach(t => t.classList.remove("active"));
            contents.forEach(c => c.classList.remove("active"));

            tab.classList.add("active");
            document.getElementById(`tab-${tab.dataset.tab}`).classList.add("active");
        });
    });

    /* ==========================================================================
       COPY & PUBLISHING INTERACTIONS
       ========================================================================== */

    // Copy to Clipboard Action
    document.querySelectorAll(".copy-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const targetId = btn.dataset.target;
            const text = document.getElementById(targetId).value;
            navigator.clipboard.writeText(text)
                .then(() => showToast("Copied post copy to clipboard!"))
                .catch(err => showToast("Failed to copy.", true));
        });
    });

    // Copy Twitter Thread
    copyTwitterBtn.addEventListener("click", () => {
        const textareas = tweetCardsContainer.querySelectorAll(".tweet-textarea");
        let fullThreadText = "";
        textareas.forEach((ta, idx) => {
            fullThreadText += `[Tweet ${idx + 1}]\n${ta.value}\n\n`;
        });
        
        navigator.clipboard.writeText(fullThreadText.trim())
            .then(() => showToast("Copied full Twitter thread to clipboard!"))
            .catch(err => showToast("Failed to copy.", true));
    });

    // Publish to LinkedIn
    publishLinkedinBtn.addEventListener("click", () => {
        const text = linkedinText.value;
        if (!text.trim()) {
            showToast("Content is empty.", true);
            return;
        }

        publishLinkedinBtn.disabled = true;
        publishLinkedinBtn.textContent = "Publishing...";

        fetch("/api/publish/linkedin", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text, video_path: uploadedVideoPath || null })
        })
        .then(res => {
            if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to publish") });
            return res.json();
        })
        .then(data => {
            showToast(data.message || "Posted to LinkedIn successfully!");
        })
        .catch(err => {
            showToast(err.message, true);
        })
        .finally(() => {
            publishLinkedinBtn.disabled = false;
            publishLinkedinBtn.textContent = "Publish to LinkedIn";
        });
    });

    // Publish Twitter Thread
    publishTwitterBtn.addEventListener("click", () => {
        const textareas = tweetCardsContainer.querySelectorAll(".tweet-textarea");
        const thread = [];
        let hasError = false;
        
        textareas.forEach(ta => {
            if (ta.value.length > 280) hasError = true;
            thread.push(ta.value);
        });

        if (hasError) {
            showToast("One of your tweets exceeds the 280 character limit.", true);
            return;
        }

        publishTwitterBtn.disabled = true;
        publishTwitterBtn.textContent = "Publishing Thread...";

        const payload = {
            is_thread: thread.length > 1,
            thread: thread,
            text: thread.length === 1 ? thread[0] : null,
            video_path: uploadedVideoPath || null
        };

        fetch("/api/publish/twitter", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
        .then(res => {
             if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to publish") });
             return res.json();
        })
        .then(data => {
             showToast(data.message || "Thread posted to X successfully!");
        })
        .catch(err => {
             showToast(err.message, true);
        })
        .finally(() => {
             publishTwitterBtn.disabled = false;
             publishTwitterBtn.textContent = "Publish Thread to X";
        });
    });

    /* ==========================================================================
       VIEW NAVIGATION
       ========================================================================== */
    const navPipeline = document.getElementById("nav-pipeline");
    const navCampaigns = document.getElementById("nav-campaigns");
    const navAutomation = document.getElementById("nav-automation");
    const navRepurposer = document.getElementById("nav-repurposer");
    const navGrowth = document.getElementById("nav-growth");
    
    const pipelineView = document.getElementById("pipeline-view");
    const campaignsView = document.getElementById("campaigns-view");
    const automationView = document.getElementById("automation-view");
    const repurposerView = document.getElementById("repurposer-view");
    const growthView = document.getElementById("growth-view");

    navPipeline.addEventListener("click", () => {
        navPipeline.classList.add("active");
        navCampaigns.classList.remove("active");
        navAutomation.classList.remove("active");
        if (navRepurposer) navRepurposer.classList.remove("active");
        if (navGrowth) navGrowth.classList.remove("active");
        pipelineView.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    navCampaigns.addEventListener("click", () => {
        navCampaigns.classList.add("active");
        navPipeline.classList.remove("active");
        navAutomation.classList.remove("active");
        if (navRepurposer) navRepurposer.classList.remove("active");
        if (navGrowth) navGrowth.classList.remove("active");
        campaignsView.scrollIntoView({ behavior: "smooth", block: "start" });
        loadCampaigns(); // Fetch campaigns JSON if not loaded
    });

    navAutomation.addEventListener("click", () => {
        navAutomation.classList.add("active");
        navPipeline.classList.remove("active");
        navCampaigns.classList.remove("active");
        if (navRepurposer) navRepurposer.classList.remove("active");
        if (navGrowth) navGrowth.classList.remove("active");
        automationView.scrollIntoView({ behavior: "smooth", block: "start" });
        fetchSettings(); // Refresh diagnostics and input states
    });

    if (navRepurposer) {
        navRepurposer.addEventListener("click", () => {
            navRepurposer.classList.add("active");
            navPipeline.classList.remove("active");
            navCampaigns.classList.remove("active");
            navAutomation.classList.remove("active");
            if (navGrowth) navGrowth.classList.remove("active");
            repurposerView.scrollIntoView({ behavior: "smooth", block: "start" });
        });
    }

    if (navGrowth) {
        navGrowth.addEventListener("click", () => {
            navGrowth.classList.add("active");
            navPipeline.classList.remove("active");
            navCampaigns.classList.remove("active");
            navAutomation.classList.remove("active");
            if (navRepurposer) navRepurposer.classList.remove("active");
            growthView.scrollIntoView({ behavior: "smooth", block: "start" });
            fetchGrowthOS();
        });
    }

    /* ==========================================================================
       VIRAL CAMPAIGNS HUB
       ========================================================================== */
    let campaignsData = [];
    let trendVideoMap = {};
    let conceptVideoMap = {};
    const campaignCardsList = document.getElementById("campaign-cards-list");
    const cWorkspacePlaceholder = document.getElementById("campaign-workspace-placeholder");
    const cWorkspaceLayout = document.getElementById("campaign-workspace-layout");
    
    const cVisualImg = document.getElementById("campaign-visual-img");
    const cVideoPrompt = document.getElementById("campaign-video-prompt");
    const copyMotionBtn = document.getElementById("copy-motion-btn");

    const cLinkedinText = document.getElementById("c-linkedin-text");
    const cInstagramText = document.getElementById("c-instagram-text");
    const cTweetCardsContainer = document.getElementById("c-tweet-cards-container");
    
    const publishCLinkedinBtn = document.getElementById("publish-c-linkedin-btn");
    const publishCTwitterBtn = document.getElementById("publish-c-twitter-btn");
    const copyCTwitterBtn = document.getElementById("copy-c-twitter-btn");

    // Campaign Social Tabs Switching
    const cTabs = document.querySelectorAll(".c-tab-btn");
    const cContents = document.querySelectorAll(".c-tab-content");

    cTabs.forEach(tab => {
        tab.addEventListener("click", () => {
            cTabs.forEach(t => t.classList.remove("active"));
            cContents.forEach(c => c.classList.remove("active"));

            tab.classList.add("active");
            document.getElementById(tab.dataset.ctab).classList.add("active");
        });
    });

    function loadCampaigns() {
        if (campaignsData.length > 0) return; // Already loaded

        fetch("/static/campaign_scripts.json")
            .then(res => res.json())
            .then(data => {
                campaignsData = data;
                renderCampaignList();
            })
            .catch(err => {
                console.error("Failed to load campaigns scripts:", err);
                showToast("Could not load viral campaigns scripts.", true);
            });
    }

    function renderCampaignList() {
        campaignCardsList.innerHTML = "";
        campaignsData.forEach(camp => {
            const card = document.createElement("div");
            card.className = "campaign-selector-card";
            card.dataset.id = camp.id;
            card.innerHTML = `
                <img src="${camp.image}" class="campaign-thumbnail" alt="${camp.title}">
                <div class="campaign-info">
                    <h4>${camp.title}</h4>
                    <p>${camp.concept}</p>
                </div>
            `;
            
            card.addEventListener("click", () => {
                document.querySelectorAll(".campaign-selector-card").forEach(c => c.classList.remove("active"));
                card.classList.add("active");
                selectCampaign(camp);
            });

            campaignCardsList.appendChild(card);
        });
    }

    function selectCampaign(camp) {
        cWorkspacePlaceholder.classList.add("hidden");
        cWorkspaceLayout.classList.remove("hidden");

        // Set visual elements
        cVisualImg.src = camp.image;
        cVideoPrompt.value = camp.video_prompt;

        // Reset Video Generator UI or load cached video
        resetConceptVideoUI(camp.id);

        // Set textareas
        cLinkedinText.value = camp.linkedin_post;
        cInstagramText.value = camp.instagram_caption;

        // Populate Twitter Thread cards
        cTweetCardsContainer.innerHTML = "";
        const tweets = camp.twitter_thread;

        tweets.forEach((tweetText, index) => {
            const card = document.createElement("div");
            card.className = "tweet-card";
            card.innerHTML = `
                <div class="tweet-header">
                    <span>Tweet ${index + 1}</span>
                    <span class="badge">Draft</span>
                </div>
                <textarea class="c-tweet-textarea" rows="4">${tweetText}</textarea>
                <div class="counter-container">
                    <span class="c-char-count">${tweetText.length}</span>/280
                </div>
            `;

            const textarea = card.querySelector(".c-tweet-textarea");
            const charCountSpan = card.querySelector(".c-char-count");
            const counterContainer = card.querySelector(".counter-container");

            textarea.addEventListener("input", () => {
                const len = textarea.value.length;
                charCountSpan.textContent = len;
                if (len > 280) {
                    counterContainer.classList.add("danger");
                } else {
                    counterContainer.classList.remove("danger");
                }
            });

            cTweetCardsContainer.appendChild(card);
        });
    }

    // Campaign Action Handlers
    copyMotionBtn.addEventListener("click", () => {
        navigator.clipboard.writeText(cVideoPrompt.value)
            .then(() => showToast("Copied Image-to-Video prompt to clipboard!"))
            .catch(err => showToast("Failed to copy.", true));
    });

    publishCLinkedinBtn.addEventListener("click", () => {
        const text = cLinkedinText.value;
        if (!text.trim()) return;

        const activeCard = document.querySelector(".campaign-selector-card.active");
        const campId = activeCard ? activeCard.dataset.id : "";
        const videoPath = conceptVideoMap[campId] || null;

        publishCLinkedinBtn.disabled = true;
        publishCLinkedinBtn.textContent = "Publishing...";

        fetch("/api/publish/linkedin", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text, video_path: videoPath })
        })
        .then(res => {
            if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to publish") });
            return res.json();
        })
        .then(data => {
            showToast(data.message || "Posted to LinkedIn successfully!");
        })
        .catch(err => {
            showToast(err.message, true);
        })
        .finally(() => {
            publishCLinkedinBtn.disabled = false;
            publishCLinkedinBtn.textContent = "Publish";
        });
    });

    copyCTwitterBtn.addEventListener("click", () => {
        const textareas = cTweetCardsContainer.querySelectorAll(".c-tweet-textarea");
        let fullThreadText = "";
        textareas.forEach((ta, idx) => {
            fullThreadText += `[Tweet ${idx + 1}]\n${ta.value}\n\n`;
        });
        
        navigator.clipboard.writeText(fullThreadText.trim())
            .then(() => showToast("Copied full Twitter thread to clipboard!"))
            .catch(err => showToast("Failed to copy.", true));
    });

    publishCTwitterBtn.addEventListener("click", () => {
        const textareas = cTweetCardsContainer.querySelectorAll(".c-tweet-textarea");
        const thread = [];
        let hasError = false;
        
        textareas.forEach(ta => {
            if (ta.value.length > 280) hasError = true;
            thread.push(ta.value);
        });

        if (hasError) {
            showToast("One of your tweets exceeds the 280 character limit.", true);
            return;
        }

        const activeCard = document.querySelector(".campaign-selector-card.active");
        const campId = activeCard ? activeCard.dataset.id : "";
        const videoPath = conceptVideoMap[campId] || null;

        publishCTwitterBtn.disabled = true;
        publishCTwitterBtn.textContent = "Publishing Thread...";

        const payload = {
            is_thread: thread.length > 1,
            thread: thread,
            text: thread.length === 1 ? thread[0] : null,
            video_path: videoPath
        };

        fetch("/api/publish/twitter", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
        .then(res => {
             if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to publish") });
             return res.json();
        })
        .then(data => {
             showToast(data.message || "Thread posted to X successfully!");
        })
        .catch(err => {
             showToast(err.message, true);
        })
        .finally(() => {
             publishCTwitterBtn.disabled = false;
             publishCTwitterBtn.textContent = "Publish Thread";
        });
    });

    /* ==========================================================================
       SUB-NAV CAMPAIGNS SWITCHING
       ========================================================================== */
    const subnavPrebuilt = document.getElementById("subnav-prebuilt");
    const subnavScanner = document.getElementById("subnav-scanner");
    const prebuiltContainer = document.getElementById("prebuilt-campaigns-container");
    const scannerContainer = document.getElementById("scanner-campaigns-container");

    subnavPrebuilt.addEventListener("click", () => {
        subnavPrebuilt.classList.add("active");
        subnavScanner.classList.remove("active");
        prebuiltContainer.classList.remove("hidden");
        scannerContainer.classList.add("hidden");
        loadCampaigns();
    });

    subnavScanner.addEventListener("click", () => {
        subnavScanner.classList.add("active");
        subnavPrebuilt.classList.remove("active");
        scannerContainer.classList.remove("hidden");
        prebuiltContainer.classList.add("hidden");
    });

    /* ==========================================================================
       LIVE TREND SCANNER FUNCTIONALITY
       ========================================================================== */
    let scannerPollInterval = null;
    let scannerActiveJobId = null;
    let trendingData = [];
    let trendActiveMode = "recreate"; // "recreate" or "repurpose"
    let currentSelectedTrend = null;
    let trendOriginalVideoMap = {};
    let trendSourceVideoMap = {};
    let trendTemplateMap = {};

    const scannerTriggerBtn = document.getElementById("scanner-trigger-btn");
    const scannerProgressCard = document.getElementById("scanner-progress-card");
    const scannerStatusMsg = document.getElementById("scanner-status-msg");
    const trendCardsList = document.getElementById("trend-cards-list");

    const trendWorkspacePlaceholder = document.getElementById("trend-workspace-placeholder");
    const trendWorkspaceLayout = document.getElementById("trend-workspace-layout");

    // Trend detail elements
    const trendTitleHeader = document.getElementById("trend-title-header");
    const trendPlatformTag = document.getElementById("trend-platform-tag");
    const trendAuthorTag = document.getElementById("trend-author-tag");
    const trendMetricsTag = document.getElementById("trend-metrics-tag");
    const trendOriginalLinkAnchor = document.getElementById("trend-original-link-anchor");
    const trendOriginalConcept = document.getElementById("trend-original-concept");
    const trendStudioTwist = document.getElementById("trend-studio-twist");
    const trendVideoPrompt = document.getElementById("trend-video-prompt");
    const copyTrendMotionBtn = document.getElementById("copy-trend-motion-btn");

    const tLinkedinText = document.getElementById("t-linkedin-text");
    const tInstagramText = document.getElementById("t-instagram-text");
    const tTweetCardsContainer = document.getElementById("t-tweet-cards-container");

    const publishTLinkedinBtn = document.getElementById("publish-t-linkedin-btn");
    const publishTTwitterBtn = document.getElementById("publish-t-twitter-btn");
    const copyTTwitterBtn = document.getElementById("copy-t-twitter-btn");

    // Trend sub-tabs
    const tTabs = document.querySelectorAll(".t-tab-btn");
    const tContents = document.querySelectorAll(".t-tab-content");

    tTabs.forEach(tab => {
        tab.addEventListener("click", () => {
            tTabs.forEach(t => t.classList.remove("active"));
            tContents.forEach(c => c.classList.remove("active"));

            tab.classList.add("active");
            document.getElementById(tab.dataset.ttab).classList.add("active");
        });
    });

    scannerTriggerBtn.addEventListener("click", () => {
        scannerTriggerBtn.disabled = true;
        scannerProgressCard.classList.remove("hidden");
        scannerStatusMsg.textContent = "Connecting to Google Search index...";
        trendCardsList.innerHTML = "";
        
        fetch("/api/viral-search", {
            method: "POST",
            headers: { "Content-Type": "application/json" }
        })
        .then(res => {
            if (!res.ok) throw new Error("Could not start search pipeline");
            return res.json();
        })
        .then(data => {
            scannerActiveJobId = data.job_id;
            scannerPollInterval = setInterval(pollScannerStatus, 3000);
        })
        .catch(err => {
            showToast("Failed to trigger live search.", true);
            resetScannerUI();
        });
    });

    function resetScannerUI() {
        scannerTriggerBtn.disabled = false;
        scannerProgressCard.classList.add("hidden");
    }

    function pollScannerStatus() {
        if (!scannerActiveJobId) return;

        fetch(`/api/viral-status/${scannerActiveJobId}`)
            .then(res => res.json())
            .then(data => {
                if (data.status === "PROCESSING" || data.status === "PENDING") {
                    scannerStatusMsg.textContent = data.message;
                } else if (data.status === "SUCCESS") {
                    clearInterval(scannerPollInterval);
                    showToast("Trend scanning completed successfully!");
                    scannerStatusMsg.textContent = "Search complete!";
                    
                    setTimeout(() => {
                        scannerProgressCard.classList.add("hidden");
                        scannerTriggerBtn.disabled = false;
                        trendingData = data.result.trends;
                        renderTrendList();
                    }, 1000);
                } else if (data.status === "FAILED") {
                    clearInterval(scannerPollInterval);
                    showToast(data.message, true);
                    resetScannerUI();
                }
            })
            .catch(err => {
                console.error("Polling scanner error:", err);
            });
    }

    function renderTrendList() {
        trendCardsList.innerHTML = "";
        if (!trendingData || trendingData.length === 0) {
            trendCardsList.innerHTML = "<p style='text-align:center; font-size:0.85rem; color:var(--text-secondary);'>No trends found. Try again.</p>";
            return;
        }

        trendingData.forEach((trend, idx) => {
            const card = document.createElement("div");
            card.className = "campaign-selector-card";
            card.dataset.idx = idx;
            
            // Format icon/badge based on platform
            const icon = `<span class="trend-platform-badge">${trend.platform}</span>`;
            
            card.innerHTML = `
                <div class="campaign-info" style="flex:1;">
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <h4 style="max-width: 180px;">${trend.title}</h4>
                        ${icon}
                    </div>
                    <p style="margin-top:0.25rem;">by ${trend.author} • ${trend.viral_metrics}</p>
                </div>
            `;

            card.addEventListener("click", () => {
                trendCardsList.querySelectorAll(".campaign-selector-card").forEach(c => c.classList.remove("active"));
                card.classList.add("active");
                selectTrend(trend);
            });

            trendCardsList.appendChild(card);
        });

        // Auto-select first card
        if (trendCardsList.children.length > 0) {
            trendCardsList.children[0].click();
        }
    }

    function triggerLoadOriginalVideo(originalUrl, trendId) {
        const placeholder = document.getElementById("trend-orig-video-placeholder");
        const loader = document.getElementById("trend-orig-video-loading");
        const statusMsg = document.getElementById("trend-orig-video-status-msg");

        if (placeholder) placeholder.classList.add("hidden");
        if (loader) loader.classList.remove("hidden");
        if (statusMsg) statusMsg.textContent = "Connecting to scraper endpoint...";

        fetch("/api/load-original-video", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: originalUrl, title: trendId })
        })
        .then(res => {
            if (!res.ok) throw new Error("Scraper failed to initialize video extraction");
            return res.json();
        })
        .then(data => {
            pollScrapedVideoStatus(data.job_id, trendId);
        })
        .catch(err => {
            showToast(err.message, true);
            resetTrendOrigVideoUI(trendId);
        });
    }

    function selectTrend(trend) {
        trendWorkspacePlaceholder.classList.add("hidden");
        trendWorkspaceLayout.classList.remove("hidden");
        currentSelectedTrend = trend;

        // Set card information
        trendTitleHeader.textContent = trend.title;
        trendPlatformTag.textContent = trend.platform;
        trendAuthorTag.textContent = `Creator: ${trend.author}`;
        trendMetricsTag.textContent = `Virality: ${trend.viral_metrics}`;
        let rawUrl = trend.url || "";
        const isMockPattern = (
            !rawUrl.startsWith("http") ||
            rawUrl.includes("google.com/search") ||
            rawUrl.includes("examplecyber") ||
            rawUrl.includes("12345") ||
            rawUrl.includes("abcdef") ||
            rawUrl.includes("status/example") ||
            rawUrl.includes("DigitalDreams") ||
            rawUrl.includes("AIVoyager")
        );
        if (isMockPattern) {
            rawUrl = "";
        }
        trendOriginalLinkAnchor.href = rawUrl || "#";
        trendOriginalLinkAnchor.style.pointerEvents = rawUrl ? "auto" : "none";
        trendOriginalLinkAnchor.style.opacity = rawUrl ? "1" : "0.45";
        trendOriginalConcept.textContent = trend.original_concept;
        trendStudioTwist.textContent = trend.studio_adaptation_concept;
        
        // Load original text field
        const origPostTextarea = document.getElementById("trend-original-post-text");
        if (origPostTextarea) {
            origPostTextarea.value = trend.original_post_text || "";
        }

        // Set active mode visuals and copy
        const recreateCont = document.getElementById("trend-recreate-container");
        const repurposeCont = document.getElementById("trend-repurpose-container");
        const modeRecBtn = document.getElementById("trend-mode-recreate");
        const modeRepBtn = document.getElementById("trend-mode-repurpose");

        if (trendActiveMode === "recreate") {
            recreateCont.classList.remove("hidden");
            repurposeCont.classList.add("hidden");
            modeRecBtn.classList.add("active");
            modeRepBtn.classList.remove("active");

            trendVideoPrompt.value = trend.recreated_video_prompt;
            tLinkedinText.value = trend.recreated_linkedin_post;
            tInstagramText.value = trend.recreated_instagram_caption;
            
            // Populate X/Twitter thread cards
            tTweetCardsContainer.innerHTML = "";
            const tweets = trend.recreated_twitter_thread || [trend.title];
            renderTrendTweets(tweets);
            
            // Reset Video Generator UI or load cached video
            resetTrendVideoUI(trend.title);
        } else {
            recreateCont.classList.add("hidden");
            repurposeCont.classList.remove("hidden");
            modeRecBtn.classList.remove("active");
            modeRepBtn.classList.add("active");

            tLinkedinText.value = trend.repurposed_6frame_linkedin_post || "";
            tInstagramText.value = trend.repurposed_6frame_instagram_caption || "";
            
            // Populate X/Twitter thread cards
            tTweetCardsContainer.innerHTML = "";
            const tweets = trend.repurposed_6frame_twitter_thread || [trend.title];
            renderTrendTweets(tweets);
            
            // Reset Original Video UI
            resetTrendOrigVideoUI(trend.title);

            // AUTO-DOWNLOAD: Trigger original video download automatically if valid URL and not already loaded/cached
            const downloadUrl = trend.url || "";
            if (downloadUrl && !trendOriginalVideoMap[trend.title]) {
                triggerLoadOriginalVideo(downloadUrl, trend.title);
            }
        }
    }

    function renderTrendTweets(tweets) {
        tweets.forEach((tweetText, index) => {
            const card = document.createElement("div");
            card.className = "tweet-card";
            card.innerHTML = `
                <div class="tweet-header">
                     <span>Tweet ${index + 1}</span>
                     <span class="badge">Draft</span>
                </div>
                <textarea class="t-tweet-textarea" rows="4">${tweetText}</textarea>
                <div class="counter-container">
                     <span class="t-char-count">${tweetText.length}</span>/280
                </div>
            `;

            const textarea = card.querySelector(".t-tweet-textarea");
            const charCountSpan = card.querySelector(".t-char-count");
            const counterContainer = card.querySelector(".counter-container");

            textarea.addEventListener("input", () => {
                const len = textarea.value.length;
                charCountSpan.textContent = len;
                if (len > 280) {
                    counterContainer.classList.add("danger");
                } else {
                    counterContainer.classList.remove("danger");
                }
            });

            tTweetCardsContainer.appendChild(card);
        });
    }

    function resetTrendOrigVideoUI(trendId) {
        const placeholder = document.getElementById("trend-orig-video-placeholder");
        const loader = document.getElementById("trend-orig-video-loading");
        const playerCont = document.getElementById("trend-orig-video-player-container");
        const player = document.getElementById("trend-orig-video-player");

        if (trendOriginalVideoMap[trendId]) {
            placeholder.classList.add("hidden");
            loader.classList.add("hidden");
            playerCont.classList.remove("hidden");
            player.src = trendOriginalVideoMap[trendId];
        } else {
            placeholder.classList.remove("hidden");
            loader.classList.add("hidden");
            playerCont.classList.add("hidden");
            player.src = "";
        }
    }

    // Trend Mode Selectors
    const modeRecBtn = document.getElementById("trend-mode-recreate");
    const modeRepBtn = document.getElementById("trend-mode-repurpose");
    const loadOrigBtn = document.getElementById("trend-load-orig-video-btn");
    const removeOrigBtn = document.getElementById("trend-orig-remove-btn");

    if (modeRecBtn && modeRepBtn) {
        modeRecBtn.addEventListener("click", () => {
            trendActiveMode = "recreate";
            if (currentSelectedTrend) selectTrend(currentSelectedTrend);
        });

        modeRepBtn.addEventListener("click", () => {
            trendActiveMode = "repurpose";
            if (currentSelectedTrend) selectTrend(currentSelectedTrend);
        });
    }

    if (loadOrigBtn) {
        loadOrigBtn.addEventListener("click", () => {
            if (!currentSelectedTrend) return;
            const trendId = currentSelectedTrend.title;
            const originalUrl = currentSelectedTrend.url;
            if (originalUrl && originalUrl.startsWith("http")) {
                triggerLoadOriginalVideo(originalUrl, trendId);
            } else {
                showToast("Cannot auto-download: link is not a direct URL.", true);
            }
        });
    }

    if (removeOrigBtn) {
        removeOrigBtn.addEventListener("click", () => {
            if (!currentSelectedTrend) return;
            const trendId = currentSelectedTrend.title;
            trendOriginalVideoMap[trendId] = null;
            resetTrendOrigVideoUI(trendId);
            showToast("Original video association removed.");
        });
    }

    function pollScrapedVideoStatus(jobId, trendId) {
        const loader = document.getElementById("trend-orig-video-loading");
        const statusMsg = document.getElementById("trend-orig-video-status-msg");

        const interval = setInterval(() => {
            fetch(`/api/video-status/${jobId}`)
                .then(res => res.json())
                .then(data => {
                    if (data.status === "PENDING" || data.status === "PROCESSING") {
                        statusMsg.textContent = `${data.message} (${data.progress}%)`;
                    } else if (data.status === "SUCCESS") {
                        clearInterval(interval);
                        trendOriginalVideoMap[trendId] = data.result.video_path;
                        showToast("Original video loaded successfully!");
                        if (currentSelectedTrend && currentSelectedTrend.title === trendId) {
                            resetTrendOrigVideoUI(trendId);
                        }
                    } else if (data.status === "FAILED") {
                        clearInterval(interval);
                        showToast(data.message || "Failed to download original video", true);
                        resetTrendOrigVideoUI(trendId);
                    }
                })
                .catch(err => {
                    console.error("Error polling scraped video status:", err);
                });
        }, 3000);
    }

    // Trend action handlers
    copyTrendMotionBtn.addEventListener("click", () => {
        navigator.clipboard.writeText(trendVideoPrompt.value)
            .then(() => showToast("Copied recreated motion prompt!"))
            .catch(err => showToast("Failed to copy.", true));
    });

    publishTLinkedinBtn.addEventListener("click", () => {
        const text = tLinkedinText.value;
        if (!text.trim()) return;

        const trendId = trendTitleHeader.textContent;
        const videoPath = trendActiveMode === "recreate" ? (trendVideoMap[trendId] || null) : (trendOriginalVideoMap[trendId] || null);

        publishTLinkedinBtn.disabled = true;
        publishTLinkedinBtn.textContent = "Publishing...";

        fetch("/api/publish/linkedin", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text, video_path: videoPath })
        })
        .then(res => {
            if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to publish") });
            return res.json();
        })
        .then(data => {
            showToast(data.message || "Posted adaptation to LinkedIn successfully!");
        })
        .catch(err => {
            showToast(err.message, true);
        })
        .finally(() => {
            publishTLinkedinBtn.disabled = false;
            publishTLinkedinBtn.textContent = "Publish";
        });
    });

    copyTTwitterBtn.addEventListener("click", () => {
        const textareas = tTweetCardsContainer.querySelectorAll(".t-tweet-textarea");
        let fullThreadText = "";
        textareas.forEach((ta, idx) => {
            fullThreadText += `[Tweet ${idx + 1}]\n${ta.value}\n\n`;
        });
        
        navigator.clipboard.writeText(fullThreadText.trim())
            .then(() => showToast("Copied full adapted Twitter thread!"))
            .catch(err => showToast("Failed to copy.", true));
    });

    publishTTwitterBtn.addEventListener("click", () => {
        const textareas = tTweetCardsContainer.querySelectorAll(".t-tweet-textarea");
        const thread = [];
        let hasError = false;
        
        textareas.forEach(ta => {
            if (ta.value.length > 280) hasError = true;
            thread.push(ta.value);
        });

        if (hasError) {
            showToast("One of your tweets exceeds the 280 character limit.", true);
            return;
        }

        const trendId = trendTitleHeader.textContent;
        const videoPath = trendActiveMode === "recreate" ? (trendVideoMap[trendId] || null) : (trendOriginalVideoMap[trendId] || null);

        publishTTwitterBtn.disabled = true;
        publishTTwitterBtn.textContent = "Publishing Thread...";

        const payload = {
            is_thread: thread.length > 1,
            thread: thread,
            text: thread.length === 1 ? thread[0] : null,
            video_path: videoPath
        };

        fetch("/api/publish/twitter", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
        .then(res => {
             if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to publish") });
             return res.json();
        })
        .then(data => {
             showToast(data.message || "Adapted thread posted to X successfully!");
        })
        .catch(err => {
             showToast(err.message, true);
        })
        .finally(() => {
             publishTTwitterBtn.disabled = false;
             publishTTwitterBtn.textContent = "Publish Thread";
        });
    });

    /* ==========================================================================
       GOOGLE VEO VIDEO GENERATOR INTEGRATION
       ========================================================================== */
    // Video Generation Elements - Trends
    const trendGenerateVideoBtn = document.getElementById("trend-generate-video-btn");
    const trendVideoPlaceholder = document.getElementById("trend-video-placeholder");
    const trendVideoLoading = document.getElementById("trend-video-loading");
    const trendVideoStatusMsg = document.getElementById("trend-video-status-msg");
    const trendVideoPlayerContainer = document.getElementById("trend-video-player-container");
    const trendVideoPlayer = document.getElementById("trend-video-player");
    const trendDownloadVideoBtn = document.getElementById("trend-download-video-btn");
    const trendApplyTemplateBtn = document.getElementById("trend-apply-template-btn");
    const trendTemplateStyleSelect = document.getElementById("trend-template-style");
    const trendTemplateLoading = document.getElementById("trend-template-loading");
    const trendTemplateStatusMsg = document.getElementById("trend-template-status-msg");

    // Video Generation Elements - Concepts
    const conceptGenerateVideoBtn = document.getElementById("concept-generate-video-btn");
    const conceptVideoPlaceholder = document.getElementById("concept-video-placeholder");
    const conceptVideoLoading = document.getElementById("concept-video-loading");
    const conceptVideoStatusMsg = document.getElementById("concept-video-status-msg");
    const conceptVideoPlayerContainer = document.getElementById("concept-video-player-container");
    const conceptVideoPlayer = document.getElementById("concept-video-player");
    const conceptDownloadVideoBtn = document.getElementById("concept-download-video-btn");

    function resetTrendVideoUI(trendId) {
        setTrendTemplateLoading(false);
        if (trendVideoMap[trendId]) {
            trendVideoPlaceholder.classList.add("hidden");
            trendVideoLoading.classList.add("hidden");
            trendVideoPlayerContainer.classList.remove("hidden");
            trendVideoPlayer.src = trendVideoMap[trendId];
            trendDownloadVideoBtn.href = trendVideoMap[trendId];
        } else {
            trendVideoPlaceholder.classList.remove("hidden");
            trendVideoLoading.classList.add("hidden");
            trendVideoPlayerContainer.classList.add("hidden");
            trendVideoPlayer.src = "";
            trendDownloadVideoBtn.href = "";
        }
    }

    function resetConceptVideoUI(campId) {
        if (conceptVideoMap[campId]) {
            conceptVideoPlaceholder.classList.add("hidden");
            conceptVideoLoading.classList.add("hidden");
            conceptVideoPlayerContainer.classList.remove("hidden");
            conceptVideoPlayer.src = conceptVideoMap[campId];
            conceptDownloadVideoBtn.href = conceptVideoMap[campId];
        } else {
            conceptVideoPlaceholder.classList.remove("hidden");
            conceptVideoLoading.classList.add("hidden");
            conceptVideoPlayerContainer.classList.add("hidden");
            conceptVideoPlayer.src = "";
            conceptDownloadVideoBtn.href = "";
        }
    }

    const trendEngineSelect = document.getElementById("trend-video-engine");
    const trendDurationSelect = document.getElementById("trend-video-duration");
    const conceptEngineSelect = document.getElementById("concept-video-engine");
    const conceptDurationSelect = document.getElementById("concept-video-duration");
    const autopilotEngineSelect = document.getElementById("autonomous-video-engine");
    const autopilotDurationSelect = document.getElementById("autonomous-video-duration");
    const cockpitAutopilotEngineSelect = document.getElementById("cockpit-autonomous-video-engine");
    const cockpitAutopilotDurationSelect = document.getElementById("cockpit-autonomous-video-duration");

    function updateDurationOptions(engineSelect, durationSelect) {
        const engine = engineSelect.value;
        const isRunway = engine === "runway_gen3";
        const isHailuo = engine === "fal_hailuo_23" || engine === "fal_hailuo_02";
        const isSeedance = engine === "fal_seedance_fast";
        const isLtx = engine === "fal_ltx_fast";
        const isVeo = engine === "google_veo" || engine === "google_veo_lite" || engine === "google_veo_fast";
        const options = durationSelect.querySelectorAll("option");
        const allowed = (
            isVeo ? ["8"] :
            isRunway ? ["10", "30"] :
            isHailuo ? ["10"] :
            isSeedance ? ["8", "10", "12"] :
            isLtx ? ["8", "10", "12", "14", "16", "18", "20"] :
            ["8", "10", "15"]
        );
        const engineDefault = isVeo ? "8" : (isHailuo ? "10" : (isRunway ? "10" : "10"));
        options.forEach(opt => {
            const value = opt.value;
            const enabled = allowed.includes(value);
            if (enabled) {
                opt.removeAttribute("disabled");
            } else {
                opt.setAttribute("disabled", "true");
            }
        });
        const currentDisabled = durationSelect.selectedOptions[0] && durationSelect.selectedOptions[0].disabled;
        if (!durationSelect.value || currentDisabled || durationSelect.value === "5") {
            durationSelect.value = allowed.includes(engineDefault) ? engineDefault : allowed[0];
        }
    }

    function getVideoEngineLabel(engine) {
        const labels = {
            google_veo: "Google Veo 3.1",
            google_veo_lite: "Google Veo 3.1 Lite",
            google_veo_fast: "Google Veo 3.1 Fast",
            runway_gen3: "Runway Gen-4.5",
            fal_hailuo_23: "FAL Hailuo 2.3",
            fal_hailuo_02: "FAL Hailuo 02",
            fal_seedance_fast: "FAL Seedance Fast",
            fal_ltx_fast: "FAL LTX Fast"
        };
        return labels[engine] || "selected engine";
    }

    if (trendEngineSelect && trendDurationSelect) {
        trendEngineSelect.addEventListener("change", () => updateDurationOptions(trendEngineSelect, trendDurationSelect));
        updateDurationOptions(trendEngineSelect, trendDurationSelect);
    }
    if (conceptEngineSelect && conceptDurationSelect) {
        conceptEngineSelect.addEventListener("change", () => updateDurationOptions(conceptEngineSelect, conceptDurationSelect));
        updateDurationOptions(conceptEngineSelect, conceptDurationSelect);
    }
    if (autopilotEngineSelect && autopilotDurationSelect) {
        autopilotEngineSelect.addEventListener("change", () => updateDurationOptions(autopilotEngineSelect, autopilotDurationSelect));
        updateDurationOptions(autopilotEngineSelect, autopilotDurationSelect);
    }
    if (cockpitAutopilotEngineSelect && cockpitAutopilotDurationSelect) {
        cockpitAutopilotEngineSelect.addEventListener("change", () => updateDurationOptions(cockpitAutopilotEngineSelect, cockpitAutopilotDurationSelect));
        updateDurationOptions(cockpitAutopilotEngineSelect, cockpitAutopilotDurationSelect);
    }

    function startTrendVideoGeneration(trendId, onSuccess) {
        const prompt = trendVideoPrompt.value;
        const engine = document.getElementById("trend-video-engine").value;
        const duration = parseInt(document.getElementById("trend-video-duration").value, 10);
        if (!prompt) return;

        trendVideoPlaceholder.classList.add("hidden");
        trendVideoLoading.classList.remove("hidden");
        
        const engineName = getVideoEngineLabel(engine);
        trendVideoStatusMsg.textContent = `Connecting to ${engineName}`;

        fetch("/api/generate-video", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt, engine, duration })
        })
        .then(res => {
            if (!res.ok) throw new Error("Failed to start video rendering");
            return res.json();
        })
        .then(data => {
            pollVideoStatus(data.job_id, "trend", trendId, onSuccess);
        })
        .catch(err => {
            showToast(err.message || "Failed to trigger video generation", true);
            resetTrendVideoUI(trendId);
        });
    }

    trendGenerateVideoBtn.addEventListener("click", () => {
        const trendId = trendTitleHeader.textContent;
        startTrendVideoGeneration(trendId);
    });

    conceptGenerateVideoBtn.addEventListener("click", () => {
        const prompt = cVideoPrompt.value;
        const campaignCard = document.querySelector(".campaign-selector-card.active");
        const campId = campaignCard ? campaignCard.dataset.id : "unknown";
        const engine = document.getElementById("concept-video-engine").value;
        const duration = parseInt(document.getElementById("concept-video-duration").value, 10);
        if (!prompt) return;

        conceptVideoPlaceholder.classList.add("hidden");
        conceptVideoLoading.classList.remove("hidden");
        
        const engineName = getVideoEngineLabel(engine);
        conceptVideoStatusMsg.textContent = `Connecting to ${engineName}`;

        fetch("/api/generate-video", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt, engine, duration })
        })
        .then(res => {
            if (!res.ok) throw new Error("Failed to start video rendering");
            return res.json();
        })
        .then(data => {
            pollVideoStatus(data.job_id, "concept", campId);
        })
        .catch(err => {
            showToast(err.message || "Failed to trigger video generation", true);
            resetConceptVideoUI(campId);
        });
    });

    /* ==========================================================================
       VIRALITY SCORE & PLATFORM VARIANTS (trend workspace)
       ========================================================================== */
    const trendViralityBtn = document.getElementById("trend-virality-score-btn");
    const trendViralityResult = document.getElementById("trend-virality-result");
    const trendGenerateVariantsBtn = document.getElementById("trend-generate-variants-btn");
    const trendVariantsLoading = document.getElementById("trend-variants-loading");
    const trendVariantsStatusMsg = document.getElementById("trend-variants-status-msg");
    const trendVariantsResult = document.getElementById("trend-variants-result");

    if (trendViralityBtn) {
        trendViralityBtn.addEventListener("click", () => {
            if (!currentSelectedTrend) { showToast("Select a trend first.", true); return; }
            trendViralityBtn.disabled = true;
            trendViralityBtn.textContent = "Scoring...";
            fetch("/api/virality-score", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    post_text: currentSelectedTrend.recreated_linkedin_post || "",
                    video_prompt: currentSelectedTrend.recreated_video_prompt || "",
                    platform: "Twitter/X and LinkedIn"
                })
            })
            .then(res => { if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to score virality") }); return res.json(); })
            .then(data => {
                const d = data.data;
                document.getElementById("trend-virality-score-value").textContent = `${d.score}/100`;
                document.getElementById("trend-virality-reasoning").textContent = d.reasoning;
                const ul = document.getElementById("trend-virality-suggestions");
                ul.innerHTML = "";
                (d.suggested_improvements || []).forEach(s => {
                    const li = document.createElement("li");
                    li.textContent = s;
                    ul.appendChild(li);
                });
                trendViralityResult.classList.remove("hidden");
            })
            .catch(err => showToast(err.message, true))
            .finally(() => {
                trendViralityBtn.disabled = false;
                trendViralityBtn.textContent = "Get Virality Score";
            });
        });
    }

    if (trendGenerateVariantsBtn) {
        trendGenerateVariantsBtn.addEventListener("click", () => {
            const trendId = trendTitleHeader.textContent;
            const videoPath = trendVideoMap[trendId];
            if (!videoPath) { showToast("Generate a video first before creating platform variants.", true); return; }

            trendGenerateVariantsBtn.disabled = true;
            trendVariantsLoading.classList.remove("hidden");
            trendVariantsResult.classList.add("hidden");
            trendVariantsResult.innerHTML = "";

            const hookText = currentSelectedTrend ? (currentSelectedTrend.title || "") : "";

            fetch("/api/generate-video-variants", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ video_path: videoPath, hook_text: hookText })
            })
            .then(res => { if (!res.ok) throw new Error("Failed to start variant generation"); return res.json(); })
            .then(data => pollVariantsStatus(data.job_id))
            .catch(err => {
                showToast(err.message, true);
                trendGenerateVariantsBtn.disabled = false;
                trendVariantsLoading.classList.add("hidden");
            });
        });
    }

    function setTrendTemplateLoading(isLoading, message) {
        if (!trendTemplateLoading || !trendApplyTemplateBtn) return;
        trendTemplateLoading.classList.toggle("hidden", !isLoading);
        trendApplyTemplateBtn.disabled = isLoading;
        trendApplyTemplateBtn.textContent = isLoading ? "Applying..." : "Apply template";
        if (message && trendTemplateStatusMsg) {
            trendTemplateStatusMsg.textContent = message;
        }
    }

    function applyTemplateToTrend(trendId, videoPath) {
        if (!currentSelectedTrend) {
            showToast("Select a trend first.", true);
            return;
        }
        if (!videoPath) {
            showToast("Generate a trend video before applying a template.", true);
            return;
        }
        if (!trendSourceVideoMap[trendId]) {
            trendSourceVideoMap[trendId] = videoPath;
        }

        const templateId = trendTemplateStyleSelect ? trendTemplateStyleSelect.value : "hook_burst";
        const subtitleParts = [
            currentSelectedTrend.platform || "Trend scan",
            currentSelectedTrend.viral_metrics || "",
            currentSelectedTrend.author ? `by ${currentSelectedTrend.author}` : ""
        ].filter(Boolean);

        setTrendTemplateLoading(true, "Queuing HyperFrames template render...");

        fetch("/api/apply-template", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                video_path: videoPath,
                template_id: templateId,
                title: currentSelectedTrend.title || trendId,
                subtitle: subtitleParts.join(" • ")
            })
        })
        .then(res => {
            if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to start template render") });
            return res.json();
        })
        .then(data => {
            pollTrendTemplateStatus(data.job_id, trendId);
        })
        .catch(err => {
            setTrendTemplateLoading(false);
            showToast(err.message || "Failed to apply template", true);
        });
    }

    if (trendApplyTemplateBtn) {
        trendApplyTemplateBtn.addEventListener("click", () => {
            if (!currentSelectedTrend) { showToast("Select a trend first.", true); return; }
            const trendId = currentSelectedTrend.title || trendTitleHeader.textContent;
            const videoPath = trendSourceVideoMap[trendId] || trendVideoMap[trendId];

            if (videoPath) {
                applyTemplateToTrend(trendId, videoPath);
                return;
            }

            showToast("No rendered trend video yet. Generating the preview first...");
            setTrendTemplateLoading(true, "Generating preview video first...");
            startTrendVideoGeneration(trendId, renderedPath => {
                applyTemplateToTrend(trendId, renderedPath);
            });
        });
    }

    function pollTrendTemplateStatus(jobId, trendId) {
        const interval = setInterval(() => {
            fetch(`/api/template-status/${jobId}`)
                .then(res => res.json())
                .then(data => {
                    if (data.status === "PENDING" || data.status === "PROCESSING") {
                        setTrendTemplateLoading(true, `${data.message} (${data.progress}%)`);
                    } else if (data.status === "SUCCESS") {
                        clearInterval(interval);
                        const videoPath = data.result.video_path;
                        trendVideoMap[trendId] = videoPath;
                        trendTemplateMap[trendId] = {
                            template_id: data.result.template_id,
                            template_label: data.result.template_label,
                            video_path: videoPath
                        };
                        setTrendTemplateLoading(false);
                        showToast(`${data.result.template_label || "Template"} applied to selected trend.`);
                        if (trendTitleHeader.textContent === trendId) {
                            resetTrendVideoUI(trendId);
                        }
                    } else if (data.status === "FAILED") {
                        clearInterval(interval);
                        setTrendTemplateLoading(false);
                        showToast(data.message || "Template render failed", true);
                    }
                })
                .catch(err => {
                    clearInterval(interval);
                    console.error("Error polling template status:", err);
                    setTrendTemplateLoading(false);
                    showToast("Template render polling error.", true);
                });
        }, 3000);
    }

    function pollVariantsStatus(jobId) {
        const interval = setInterval(() => {
            fetch(`/api/video-variants-status/${jobId}`)
                .then(res => res.json())
                .then(data => {
                    if (data.status === "PENDING" || data.status === "PROCESSING") {
                        trendVariantsStatusMsg.textContent = `${data.message} (${data.progress}%)`;
                    } else if (data.status === "SUCCESS") {
                        clearInterval(interval);
                        trendVariantsLoading.classList.add("hidden");
                        trendGenerateVariantsBtn.disabled = false;
                        showToast("Platform variants ready!");
                        const variants = data.result.variants;
                        trendVariantsResult.innerHTML = "";
                        const labels = { vertical_9x16: "9:16 Vertical (Reels/TikTok/Shorts)", square_1x1: "1:1 Square", landscape_16x9: "16:9 Landscape" };
                        Object.entries(variants).forEach(([key, path]) => {
                            const a = document.createElement("a");
                            a.href = path;
                            a.download = true;
                            a.className = "secondary-btn";
                            a.style.cssText = "display:block; text-decoration:none; text-align:center; font-size:0.7rem; padding:0.4rem;";
                            a.textContent = `Download ${labels[key] || key}`;
                            trendVariantsResult.appendChild(a);
                        });
                        trendVariantsResult.classList.remove("hidden");
                    } else if (data.status === "FAILED") {
                        clearInterval(interval);
                        trendVariantsLoading.classList.add("hidden");
                        trendGenerateVariantsBtn.disabled = false;
                        showToast(`Variant generation failed: ${data.message}`, true);
                    }
                })
                .catch(() => {
                    clearInterval(interval);
                    trendVariantsLoading.classList.add("hidden");
                    trendGenerateVariantsBtn.disabled = false;
                    showToast("Variant generation polling error.", true);
                });
        }, 4000);
    }

    function pollVideoStatus(jobId, type, id, onSuccess) {
        const interval = setInterval(() => {
            fetch(`/api/video-status/${jobId}`)
                .then(res => res.json())
                .then(data => {
                    const statusMsgEl = type === "trend" ? trendVideoStatusMsg : conceptVideoStatusMsg;
                    
                    if (data.status === "PENDING" || data.status === "PROCESSING") {
                        statusMsgEl.textContent = `${data.message} (${data.progress}%)`;
                    } else if (data.status === "SUCCESS") {
                        clearInterval(interval);
                        const videoPath = data.result.video_path;
                        
                        if (type === "trend") {
                            trendSourceVideoMap[id] = videoPath;
                            trendTemplateMap[id] = null;
                            trendVideoMap[id] = videoPath;
                            showToast("Video rendered successfully!");
                            if (trendTitleHeader.textContent === id) {
                                resetTrendVideoUI(id);
                            }
                            if (typeof onSuccess === "function") {
                                onSuccess(videoPath);
                            }
                        } else {
                            conceptVideoMap[id] = videoPath;
                            showToast("Video rendered successfully!");
                            const activeCard = document.querySelector(".campaign-selector-card.active");
                            const currentId = activeCard ? activeCard.dataset.id : "";
                            if (currentId === id) {
                                resetConceptVideoUI(id);
                            }
                        }
                    } else if (data.status === "FAILED") {
                        clearInterval(interval);
                        showToast(data.message || "Video generation failed", true);
                        if (type === "trend") {
                            setTrendTemplateLoading(false);
                            resetTrendVideoUI(id);
                        } else {
                            resetConceptVideoUI(id);
                        }
                    }
                })
                .catch(err => {
                    console.error("Error polling video status:", err);
                });
        }, 5000);
    }

    /* ==========================================================================
       POST SCHEDULER & QUEUE LOGIC
       ========================================================================== */

    const conceptScheduleBtn = document.getElementById("concept-schedule-btn");
    const conceptScheduleTime = document.getElementById("concept-schedule-time");
    const conceptSchedulePlatform = document.getElementById("concept-schedule-platform");

    const trendScheduleBtn = document.getElementById("trend-schedule-btn");
    const trendScheduleTime = document.getElementById("trend-schedule-time");
    const trendSchedulePlatform = document.getElementById("trend-schedule-platform");

    // Fetch and render the scheduled posts queue
    function fetchScheduledQueue() {
        fetch("/api/scheduled-queue")
            .then(res => res.json())
            .then(posts => {
                renderQueue(posts, "concept-queue-list");
                renderQueue(posts, "trend-queue-list");
                renderQueue(posts, "cockpit-queue-list");
            })
            .catch(err => {
                console.error("Error fetching scheduled queue:", err);
            });
    }

    function renderQueue(posts, elementId) {
        const container = document.getElementById(elementId);
        if (!container) return;
        
        if (!posts || posts.length === 0) {
            container.innerHTML = `<p style="font-size: 0.75rem; color: var(--text-secondary); text-align: center; margin: 0.5rem 0;">No scheduled posts.</p>`;
            return;
        }
        
        container.innerHTML = "";
        posts.forEach(post => {
            let statusBg = "rgba(255, 193, 7, 0.2)";
            let statusColor = "#ffc107";
            if (post.status === "SUCCESS") {
                statusBg = "rgba(40, 167, 69, 0.2)";
                statusColor = "#28a745";
            } else if (post.status === "FAILED") {
                statusBg = "rgba(220, 53, 69, 0.2)";
                statusColor = "#dc3545";
            } else if (post.status === "PARTIAL_SUCCESS") {
                statusBg = "rgba(253, 126, 20, 0.2)";
                statusColor = "#fd7e14";
            } else if (post.status === "PUBLISHING") {
                statusBg = "rgba(0, 123, 255, 0.2)";
                statusColor = "#007bff";
            }
            
            const formatPlatform = (p) => {
                if (p === "both") return "X & LinkedIn";
                if (p === "all") return "All Platforms";
                const labels = {
                    twitter: "Twitter / X", linkedin: "LinkedIn", instagram: "Instagram",
                    tiktok: "TikTok", youtube: "YouTube", facebook: "Facebook", threads: "Threads"
                };
                return p.split(",").map(part => labels[part.trim()] || part.trim()).join(", ");
            };
            
            const formatTime = (t) => {
                try {
                    return new Date(t).toLocaleString(undefined, {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit"
                    });
                } catch (e) {
                    return t;
                }
            };
            
            const item = document.createElement("div");
            item.className = "queue-item";
            item.style.cssText = "background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; padding: 0.6rem; display: flex; flex-direction: column; gap: 0.25rem; transition: background 0.2s;";
            
            item.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem;">
                    <span style="font-size: 0.75rem; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 130px;" title="${escapeHtml(post.campaign_title || 'Post')}">${escapeHtml(post.campaign_title || 'Post')}</span>
                    <span class="badge" style="font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: 4px; font-weight: bold; background: ${statusBg}; color: ${statusColor};">${post.status}</span>
                </div>
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.65rem; color: var(--text-secondary);">
                    <span>${formatPlatform(post.platform)} • ${formatTime(post.scheduled_time)}</span>
                    ${post.status === 'PENDING' ? `<button class="cancel-post-btn" data-id="${post.id}" style="background: transparent; border: none; color: #ff5555; cursor: pointer; padding: 0; font-size: 0.65rem; font-weight: 500;">Cancel</button>` : ''}
                </div>
                ${post.error_message ? `
                    <details style="margin-top: 0.35rem; font-size: 0.62rem; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 0.35rem;">
                        <summary style="color: #ff6b6b; cursor: pointer; outline: none; font-weight: 500; margin-bottom: 0.2rem; user-select: none;">
                            Show Error Log
                        </summary>
                        <div style="color: rgba(255, 107, 107, 0.95); word-break: break-all; background: rgba(255, 107, 107, 0.07); border: 1px solid rgba(255, 107, 107, 0.12); border-radius: 4px; padding: 0.4rem; max-height: 85px; overflow-y: auto; font-family: monospace; line-height: 1.25; font-size: 0.58rem;">
                            ${escapeHtml(post.error_message)}
                        </div>
                    </details>
                ` : ''}
            `;
            
            container.appendChild(item);
        });
    }

    /* ==========================================================================
       PENDING APPROVAL QUEUE (Autopilot guardrail review step)
       ========================================================================== */
    const pendingApprovalList = document.getElementById("pending-approval-list");
    const pendingApprovalCount = document.getElementById("pending-approval-count");

    function fetchApprovalQueue() {
        if (!pendingApprovalList) return;
        // Don't clobber an in-progress edit: skip this poll if focus is inside the queue.
        if (pendingApprovalList.contains(document.activeElement)) return;
        fetch("/api/approval-queue")
            .then(res => res.json())
            .then(posts => renderApprovalQueue(posts))
            .catch(err => console.error("Error fetching approval queue:", err));
    }

    function renderApprovalQueue(posts) {
        if (pendingApprovalCount) pendingApprovalCount.textContent = posts.length;
        if (!posts || posts.length === 0) {
            pendingApprovalList.innerHTML = `<p style="font-size: 0.75rem; color: var(--text-secondary); text-align: center; margin: 0.5rem 0;">No posts awaiting approval.</p>`;
            return;
        }
        pendingApprovalList.innerHTML = "";
        posts.forEach(post => {
            const card = document.createElement("div");
            card.style.cssText = "background: rgba(0,0,0,0.2); border: 1px solid rgba(255,193,7,0.2); border-radius: 8px; padding: 1rem;";
            const threadHtml = (post.thread || []).map((t, idx) => `Tweet ${idx + 1}: ${t}`).join("\n\n");
            card.innerHTML = `
                <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:0.5rem; margin-bottom:0.5rem;">
                    <strong style="font-size:0.85rem; color:var(--text-primary);">${post.campaign_title || 'Autopilot Pick'}</strong>
                    <span style="font-size:0.65rem; color:var(--text-secondary);">${post.platform}</span>
                </div>
                ${post.video_path ? `<video src="${post.video_path}" controls style="width:100%; max-width:320px; border-radius:6px; margin-bottom:0.5rem; display:block;"></video>` : ''}
                <label style="font-size:0.65rem; color:var(--text-secondary); display:block; margin-bottom:0.2rem;">LinkedIn / primary copy</label>
                <textarea class="approval-text-edit" data-id="${post.id}" rows="4" style="width:100%; background:rgba(0,0,0,0.35); border:1px solid var(--border-color); color:var(--text-primary); border-radius:6px; padding:0.5rem; font-size:0.75rem; margin-bottom:0.5rem;">${post.text || ''}</textarea>
                ${post.thread ? `
                <label style="font-size:0.65rem; color:var(--text-secondary); display:block; margin-bottom:0.2rem;">Twitter/X thread</label>
                <textarea class="approval-thread-edit" data-id="${post.id}" rows="4" style="width:100%; background:rgba(0,0,0,0.35); border:1px solid var(--border-color); color:var(--text-primary); border-radius:6px; padding:0.5rem; font-size:0.75rem; margin-bottom:0.5rem;">${threadHtml}</textarea>
                ` : ''}
                <div style="display:flex; gap:0.5rem;">
                    <button class="secondary-btn approval-save-btn" data-id="${post.id}" style="flex:1; font-size:0.75rem; padding:0.4rem;">Save Edits</button>
                    <button class="secondary-btn approval-reject-btn" data-id="${post.id}" style="flex:1; font-size:0.75rem; padding:0.4rem; color:#ff6b6b;">Reject</button>
                    <button class="primary-btn approval-approve-btn" data-id="${post.id}" style="flex:1.5; font-size:0.75rem; padding:0.4rem;">Approve &amp; Publish</button>
                </div>
            `;
            pendingApprovalList.appendChild(card);
        });
    }

    /* ==========================================================================
       PERFORMANCE ANALYTICS (feedback loop)
       ========================================================================== */
    const analyticsPostsList = document.getElementById("analytics-posts-list");
    const analyticsBestHour = document.getElementById("analytics-best-hour");
    const analyticsApplyHourBtn = document.getElementById("analytics-apply-hour-btn");
    const analyticsRefreshBtn = document.getElementById("analytics-refresh-btn");
    let analyticsRecommendedHour = null;

    function formatHourLabel(h) {
        const period = h >= 12 ? "PM" : "AM";
        const displayHour = h % 12 === 0 ? 12 : h % 12;
        return `${displayHour}:00 ${period}`;
    }

    function fetchAnalyticsSummary() {
        if (!analyticsPostsList) return;
        fetch("/api/analytics/summary")
            .then(res => res.json())
            .then(data => renderAnalyticsSummary(data))
            .catch(err => console.error("Error fetching analytics summary:", err));
    }

    function renderAnalyticsSummary(data) {
        if (data.best_hour !== null && data.best_hour !== undefined) {
            analyticsRecommendedHour = data.best_hour;
            analyticsBestHour.textContent = `${formatHourLabel(data.best_hour)} (${data.sample_size} post${data.sample_size === 1 ? '' : 's'} analyzed)`;
            if (analyticsApplyHourBtn) analyticsApplyHourBtn.disabled = false;
        } else {
            analyticsRecommendedHour = null;
            analyticsBestHour.textContent = "Not enough data yet";
            if (analyticsApplyHourBtn) analyticsApplyHourBtn.disabled = true;
        }

        if (!data.posts || data.posts.length === 0) {
            analyticsPostsList.innerHTML = `<p style="font-size: 0.75rem; color: var(--text-secondary); text-align: center; margin: 0.5rem 0;">No performance data yet — publish a post and check back after it's had time to gather engagement.</p>`;
            return;
        }

        analyticsPostsList.innerHTML = "";
        data.posts.slice(0, 10).forEach(post => {
            const row = document.createElement("div");
            row.style.cssText = "display:flex; justify-content:space-between; align-items:center; background:rgba(0,0,0,0.15); border:1px solid rgba(255,255,255,0.05); border-radius:6px; padding:0.5rem 0.75rem; font-size:0.75rem;";
            const m = post.metrics || {};
            row.innerHTML = `
                <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:45%; color:var(--text-primary);" title="${post.campaign_title || ''}">${post.campaign_title || 'Post'}</span>
                <span style="color:var(--text-secondary);">❤ ${m.like_count || 0} · 🔁 ${m.retweet_count || 0} · 💬 ${m.reply_count || 0}</span>
                <span style="color: var(--purple-light, var(--primary-color)); font-weight:600;">Score: ${post.engagement_score}</span>
            `;
            analyticsPostsList.appendChild(row);
        });
    }

    if (analyticsRefreshBtn) {
        analyticsRefreshBtn.addEventListener("click", () => {
            analyticsRefreshBtn.disabled = true;
            analyticsRefreshBtn.textContent = "Refreshing...";
            fetch("/api/analytics/refresh", { method: "POST" })
                .then(res => { if (!res.ok) throw new Error("Failed to trigger refresh"); return res.json(); })
                .then(() => {
                    showToast("Metrics refresh triggered — results may take a few seconds.");
                    setTimeout(fetchAnalyticsSummary, 3000);
                })
                .catch(err => showToast(err.message, true))
                .finally(() => {
                    analyticsRefreshBtn.disabled = false;
                    analyticsRefreshBtn.textContent = "Refresh Metrics";
                });
        });
    }

    if (analyticsApplyHourBtn) {
        analyticsApplyHourBtn.addEventListener("click", () => {
            if (analyticsRecommendedHour === null) return;
            fetch("/api/analytics/apply-best-hour", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ hour: analyticsRecommendedHour })
            })
            .then(res => { if (!res.ok) throw new Error("Failed to apply recommended hour"); return res.json(); })
            .then((data) => { showToast(data.message); fetchSettings(); })
            .catch(err => showToast(err.message, true));
        });
    }

    /* ==========================================================================
       ENGAGEMENT INBOX (AI-drafted mention replies)
       ========================================================================== */
    const engagementList = document.getElementById("engagement-list");
    const engagementCount = document.getElementById("engagement-count");
    const engagementRefreshBtn = document.getElementById("engagement-refresh-btn");

    function fetchEngagementQueue() {
        if (!engagementList) return;
        if (engagementList.contains(document.activeElement)) return;
        fetch("/api/engagement-queue")
            .then(res => res.json())
            .then(items => renderEngagementQueue(items))
            .catch(err => console.error("Error fetching engagement queue:", err));
    }

    function renderEngagementQueue(items) {
        if (engagementCount) engagementCount.textContent = items.length;
        if (!items || items.length === 0) {
            engagementList.innerHTML = `<p style="font-size: 0.75rem; color: var(--text-secondary); text-align: center; margin: 0.5rem 0;">No new mentions to review.</p>`;
            return;
        }
        engagementList.innerHTML = "";
        items.forEach(item => {
            const card = document.createElement("div");
            card.style.cssText = "background: rgba(0,0,0,0.2); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 0.9rem;";
            card.innerHTML = `
                <p style="font-size: 0.7rem; color: var(--text-secondary); margin-bottom: 0.5rem;">
                    <strong style="color: var(--text-primary);">@${escapeHtml(item.source_author)}</strong> wrote: "${escapeHtml(item.source_text)}"
                </p>
                <textarea class="engagement-reply-edit" data-id="${item.id}" rows="2" style="width:100%; background:rgba(0,0,0,0.35); border:1px solid var(--border-color); color:var(--text-primary); border-radius:6px; padding:0.5rem; font-size:0.75rem; margin-bottom:0.5rem;">${escapeHtml(item.drafted_reply)}</textarea>
                <div style="display:flex; gap:0.5rem;">
                    <button class="secondary-btn engagement-save-btn" data-id="${item.id}" style="flex:1; font-size:0.7rem; padding:0.4rem;">Save Edit</button>
                    <button class="secondary-btn engagement-dismiss-btn" data-id="${item.id}" style="flex:1; font-size:0.7rem; padding:0.4rem; color:#ff6b6b;">Dismiss</button>
                    <button class="primary-btn engagement-send-btn" data-id="${item.id}" style="flex:1.5; font-size:0.7rem; padding:0.4rem;">Send Reply</button>
                </div>
            `;
            engagementList.appendChild(card);
        });
    }

    if (engagementRefreshBtn) {
        engagementRefreshBtn.addEventListener("click", () => {
            engagementRefreshBtn.disabled = true;
            engagementRefreshBtn.textContent = "Checking...";
            fetch("/api/engagement-queue/refresh", { method: "POST" })
                .then(res => { if (!res.ok) throw new Error("Failed to check for mentions"); return res.json(); })
                .then(() => {
                    showToast("Checking for new mentions — results may take a few seconds.");
                    setTimeout(fetchEngagementQueue, 4000);
                })
                .catch(err => showToast(err.message, true))
                .finally(() => {
                    engagementRefreshBtn.disabled = false;
                    engagementRefreshBtn.textContent = "Check for New Mentions";
                });
        });
    }

    if (engagementList) {
        engagementList.addEventListener("click", (e) => {
            const id = e.target.dataset.id;
            if (!id) return;
            const card = e.target.closest("div").parentElement;

            if (e.target.classList.contains("engagement-save-btn")) {
                const textEl = card.querySelector(".engagement-reply-edit");
                fetch(`/api/engagement-queue/${id}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ reply_text: textEl.value })
                })
                .then(res => { if (!res.ok) throw new Error("Failed to save edit"); return res.json(); })
                .then(() => showToast("Draft reply updated."))
                .catch(err => showToast(err.message, true));
            }

            if (e.target.classList.contains("engagement-dismiss-btn")) {
                e.target.disabled = true;
                fetch(`/api/engagement-queue/${id}/dismiss`, { method: "POST" })
                    .then(res => { if (!res.ok) throw new Error("Failed to dismiss reply"); return res.json(); })
                    .then(() => { showToast("Reply dismissed."); fetchEngagementQueue(); })
                    .catch(err => { showToast(err.message, true); e.target.disabled = false; });
            }

            if (e.target.classList.contains("engagement-send-btn")) {
                if (!confirm("Send this reply now?")) return;
                e.target.disabled = true;
                e.target.textContent = "Sending...";
                fetch(`/api/engagement-queue/${id}/send`, { method: "POST" })
                    .then(res => { if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to send reply") }); return res.json(); })
                    .then((data) => { showToast(data.message || "Reply sent!"); fetchEngagementQueue(); })
                    .catch(err => {
                        showToast(err.message, true);
                        e.target.disabled = false;
                        e.target.textContent = "Send Reply";
                    });
            }
        });
    }

    if (pendingApprovalList) {
        pendingApprovalList.addEventListener("click", (e) => {
            const id = e.target.dataset.id;
            if (!id) return;

            if (e.target.classList.contains("approval-save-btn")) {
                const card = e.target.closest("div").parentElement;
                const textEl = card.querySelector(".approval-text-edit");
                const threadEl = card.querySelector(".approval-thread-edit");
                const payload = { text: textEl ? textEl.value : undefined };
                if (threadEl) {
                    payload.thread = threadEl.value.split(/\n\s*\n/).map(t => t.replace(/^Tweet \d+:\s*/, "").trim()).filter(Boolean);
                }
                fetch(`/api/approval-queue/${id}`, {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                })
                .then(res => { if (!res.ok) throw new Error("Failed to save edits"); return res.json(); })
                .then(() => showToast("Draft updated."))
                .catch(err => showToast(err.message, true));
            }

            if (e.target.classList.contains("approval-reject-btn")) {
                if (!confirm("Reject this autopilot pick? It will not be published.")) return;
                e.target.disabled = true;
                fetch(`/api/approval-queue/${id}/reject`, { method: "POST" })
                    .then(res => { if (!res.ok) throw new Error("Failed to reject post"); return res.json(); })
                    .then(() => { showToast("Post rejected."); fetchApprovalQueue(); fetchScheduledQueue(); })
                    .catch(err => { showToast(err.message, true); e.target.disabled = false; });
            }

            if (e.target.classList.contains("approval-approve-btn")) {
                if (!confirm("Publish this autopilot pick now?")) return;
                e.target.disabled = true;
                e.target.textContent = "Publishing...";
                fetch(`/api/approval-queue/${id}/approve`, { method: "POST" })
                    .then(res => { if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to approve post") }); return res.json(); })
                    .then((data) => { showToast(data.message || "Post approved and published!"); fetchApprovalQueue(); fetchScheduledQueue(); })
                    .catch(err => {
                        showToast(err.message, true);
                        e.target.disabled = false;
                        e.target.textContent = "Approve & Publish";
                    });
            }
        });
    }

    function cancelScheduledPost(postId) {
        if (!confirm("Are you sure you want to cancel this scheduled post?")) return;
        
        fetch(`/api/scheduled-queue/${postId}`, { method: "DELETE" })
            .then(res => {
                if (!res.ok) throw new Error("Failed to cancel scheduled post");
                return res.json();
            })
            .then(data => {
                showToast("Scheduled post cancelled.");
                fetchScheduledQueue();
            })
            .catch(err => {
                showToast(err.message, true);
            });
    }

    // Cancel action delegation
    document.getElementById("concept-queue-list").addEventListener("click", (e) => {
        if (e.target.classList.contains("cancel-post-btn")) {
            cancelScheduledPost(e.target.dataset.id);
        }
    });

    document.getElementById("trend-queue-list").addEventListener("click", (e) => {
        if (e.target.classList.contains("cancel-post-btn")) {
            cancelScheduledPost(e.target.dataset.id);
        }
    });

    // Schedule concept release event listener
    conceptScheduleBtn.addEventListener("click", () => {
        const activeCard = document.querySelector(".campaign-selector-card.active");
        if (!activeCard) {
            showToast("Please select a campaign concept first.", true);
            return;
        }
        
        const campId = activeCard.dataset.id;
        const timeVal = conceptScheduleTime.value;
        if (!timeVal) {
            showToast("Please select a scheduled release date and time.", true);
            return;
        }
        
        const platform = conceptSchedulePlatform.value;
        const campaignTitle = activeCard.querySelector("h4").textContent;
        
        // Extract texts
        const text = cLinkedinText.value;
        const textareas = cTweetCardsContainer.querySelectorAll(".c-tweet-textarea");
        const thread = [];
        let hasError = false;
        
        textareas.forEach(ta => {
            if (ta.value.length > 280) hasError = true;
            thread.push(ta.value);
        });

        if (hasError) {
            showToast("One of your tweets exceeds the 280 character limit.", true);
            return;
        }

        const videoPath = conceptVideoMap[campId] || null;
        
        conceptScheduleBtn.disabled = true;
        conceptScheduleBtn.textContent = "Scheduling...";
        
        const payload = {
            platform: platform,
            text: text,
            thread: thread.length > 0 ? thread : null,
            scheduled_time: timeVal,
            campaign_title: campaignTitle,
            video_path: videoPath
        };
        
        fetch("/api/schedule-post", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
        .then(res => {
            if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to schedule post") });
            return res.json();
        })
        .then(data => {
            showToast("Post scheduled successfully!");
            conceptScheduleTime.value = "";
            fetchScheduledQueue();
        })
        .catch(err => {
            showToast(err.message, true);
        })
        .finally(() => {
            conceptScheduleBtn.disabled = false;
            conceptScheduleBtn.textContent = "Schedule Release";
        });
    });

    // Schedule trend release event listener
    trendScheduleBtn.addEventListener("click", () => {
        const trendId = trendTitleHeader.textContent;
        if (!trendId) {
            showToast("Please select a trending topic first.", true);
            return;
        }
        
        const timeVal = trendScheduleTime.value;
        if (!timeVal) {
            showToast("Please select a scheduled release date and time.", true);
            return;
        }
        
        const platform = trendSchedulePlatform.value;
        const campaignTitle = "Trend: " + trendId;
        
        // Extract texts
        const text = tLinkedinText.value;
        const textareas = tTweetCardsContainer.querySelectorAll(".t-tweet-textarea");
        const thread = [];
        let hasError = false;
        
        textareas.forEach(ta => {
            if (ta.value.length > 280) hasError = true;
            thread.push(ta.value);
        });

        if (hasError) {
            showToast("One of your tweets exceeds the 280 character limit.", true);
            return;
        }

        const videoPath = trendActiveMode === "recreate" ? (trendVideoMap[trendId] || null) : (trendOriginalVideoMap[trendId] || null);
        
        trendScheduleBtn.disabled = true;
        trendScheduleBtn.textContent = "Scheduling...";
        
        const payload = {
            platform: platform,
            text: text,
            thread: thread.length > 0 ? thread : null,
            scheduled_time: timeVal,
            campaign_title: campaignTitle,
            video_path: videoPath
        };
        
        fetch("/api/schedule-post", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        })
        .then(res => {
            if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to schedule post") });
            return res.json();
        })
        .then(data => {
            showToast("Post scheduled successfully!");
            trendScheduleTime.value = "";
            fetchScheduledQueue();
        })
        .catch(err => {
            showToast(err.message, true);
        })
        .finally(() => {
            trendScheduleBtn.disabled = false;
            trendScheduleBtn.textContent = "Schedule Release";
        });
    });

    // Cockpit Save Settings Listener
    const cockpitSaveBtn = document.getElementById("cockpit-save-settings-btn");
    if (cockpitSaveBtn) {
        cockpitSaveBtn.addEventListener("click", () => {
            const autoPlatforms = collectAutoPlatforms("cockpit-auto-platform");

            const payload = {
                gemini_api_key: document.getElementById("cockpit-gemini-key").value,
                runway_api_key: document.getElementById("cockpit-runway-key").value,
                fal_api_key: document.getElementById("cockpit-fal-key").value,
                brand_voice: document.getElementById("cockpit-brand-voice").value,
                twitter_consumer_key: twConsumerKey.value,
                twitter_consumer_secret: twConsumerSecret.value,
                twitter_access_token: twAccessToken.value,
                twitter_access_token_secret: twAccessSecret.value,
                linkedin_access_token: liAccessToken.value,
                linkedin_person_urn: liPersonUrn.value,
                mock_mode: document.getElementById("cockpit-mock-mode").checked,
                autonomous_posting: document.getElementById("cockpit-autonomous-posting").checked,
                autonomous_hour: parseInt(document.getElementById("cockpit-autonomous-hour").value, 10),
                autonomous_platforms: autoPlatforms,
                autonomous_video_engine: document.getElementById("cockpit-autonomous-video-engine").value,
                autonomous_video_duration: parseInt(document.getElementById("cockpit-autonomous-video-duration").value, 10),
                require_autopilot_approval: document.getElementById("cockpit-require-approval").checked,
                viral_template_enabled: document.getElementById("cockpit-viral-template-enabled").checked,
                viral_template_style: document.getElementById("cockpit-viral-template-style").value,
                viral_template_quality: document.getElementById("cockpit-viral-template-quality").value,
                ...collectAdditionalPlatformCreds(),
                postproxy_daily_publish_limit: Math.max(1, parseInt(document.getElementById("cockpit-postproxy-daily-publish-limit").value || "2", 10))
            };

            cockpitSaveBtn.disabled = true;
            cockpitSaveBtn.textContent = "Saving...";

            fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            })
            .then(res => {
                if (res.ok) {
                    showToast("Cockpit configuration saved!");
                    fetchSettings(); // Refresh badges and sync layouts
                } else {
                    throw new Error("Failed to save settings");
                }
            })
            .catch(err => {
                showToast("Failed to save settings.", true);
            })
            .finally(() => {
                cockpitSaveBtn.disabled = false;
                cockpitSaveBtn.textContent = "Save Credentials & Voice";
            });
        });
    }

    // Cockpit Instant Autopilot Trigger Listener
    const cockpitTriggerBtn = document.getElementById("cockpit-trigger-pipeline-btn");
    if (cockpitTriggerBtn) {
        cockpitTriggerBtn.addEventListener("click", () => {
            cockpitTriggerBtn.disabled = true;
            cockpitTriggerBtn.textContent = "Pipeline Running...";
            showToast("Triggering autonomous scanning and rendering in background...");

            fetch("/api/trigger-autopilot", { method: "POST" })
                .then(res => {
                    if (!res.ok) throw new Error("Failed to trigger pipeline");
                    return res.json();
                })
                .then(data => {
                    showToast("Pipeline initiated successfully! Check queue for details.");
                })
                .catch(err => {
                    showToast(err.message || "Failed to trigger autopilot pipeline.", true);
                })
                .finally(() => {
                    setTimeout(() => {
                        cockpitTriggerBtn.disabled = false;
                        cockpitTriggerBtn.textContent = "Run Autopilot Pipeline Now";
                    }, 5000);
                });
        });
    }

    // Cockpit queue cancellation delegation
    const cockpitQueueList = document.getElementById("cockpit-queue-list");
    if (cockpitQueueList) {
        cockpitQueueList.addEventListener("click", (e) => {
            if (e.target.classList.contains("cancel-post-btn")) {
                cancelScheduledPost(e.target.dataset.id);
            }
        });
    }

    // Onboarding Wizard Listeners
    const wizardPostingCheck = document.getElementById("wizard-autonomous-posting");
    if (wizardPostingCheck) {
        wizardPostingCheck.addEventListener("change", () => {
            // Sync toggles
            document.getElementById("cockpit-autonomous-posting").checked = wizardPostingCheck.checked;
            document.getElementById("autonomous-posting").checked = wizardPostingCheck.checked;
            
            // Programmatically save settings to backend
            document.getElementById("cockpit-save-settings-btn").click();
        });
    }

    const wizardRunBtn = document.getElementById("wizard-run-now-btn");
    if (wizardRunBtn) {
        wizardRunBtn.addEventListener("click", () => {
            document.getElementById("cockpit-trigger-pipeline-btn").click();
        });
    }

    const wizardConfigBtn = document.getElementById("wizard-configure-btn");
    if (wizardConfigBtn) {
        wizardConfigBtn.addEventListener("click", () => {
            document.getElementById("cockpit-gemini-key").scrollIntoView({ behavior: "smooth", block: "center" });
        });
    }

    /* ==========================================================================
       VIRAL VIDEO REPURPOSER WORKSPACE
       ========================================================================== */
    // Repurposer Workshop tabs toggling
    const wTabBtns = document.querySelectorAll(".w-tab-btn");
    wTabBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            wTabBtns.forEach(b => b.classList.remove("active"));
            document.querySelectorAll(".w-tab-content").forEach(c => c.classList.remove("active"));
            
            btn.classList.add("active");
            const targetId = btn.dataset.wtab;
            document.getElementById(targetId).classList.add("active");
        });
    });

    // Scraper triggers
    const repurposeRunBtn = document.getElementById("repurpose-run-btn");
    const repurposeInputUrl = document.getElementById("repurpose-input-url");
    const repurposeGlobalLoader = document.getElementById("repurpose-global-loader");
    const repurposeWorkspace = document.getElementById("repurpose-workspace");

    let repurposerActiveVideoPath = null;
    let repurposerScrapedData = null;
    let repurposerVideoFailureMessage = null;

    if (repurposeRunBtn) {
        repurposeRunBtn.addEventListener("click", () => {
            const url = repurposeInputUrl.value.trim();
            if (!url) {
                showToast("Please enter a video URL first.", true);
                return;
            }

            // Show global loader, hide workspace
            repurposeGlobalLoader.classList.remove("hidden");
            repurposeWorkspace.classList.add("hidden");
            document.getElementById("repurpose-loader-title").textContent = "Analyzing & Grounding URL...";
            document.getElementById("repurpose-loader-status").textContent = "Crawling metadata and extracting creator details via Gemini Pro Search...";
            repurposeRunBtn.disabled = true;

            repurposerActiveVideoPath = null;
            repurposerScrapedData = null;
            repurposerVideoFailureMessage = null;

            // Step 1: Repurpose copywriting
            fetch("/api/repurpose-video-link", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url: url })
            })
            .then(res => {
                if (!res.ok) throw new Error("Copy generation failed");
                return res.json();
            })
            .then(data => {
                repurposerScrapedData = data.data;
                checkRepurposeWorkshopReady(url);
            })
            .catch(err => {
                showToast(err.message, true);
                repurposeGlobalLoader.classList.add("hidden");
                repurposeRunBtn.disabled = false;
            });

            fetch("/api/load-original-video", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url: url, allow_fallback: false })
            })
            .then(res => {
                if (!res.ok) throw new Error("Video download registration failed");
                return res.json();
            })
            .then(data => {
                pollRepurposeVideoStatus(data.job_id, url);
            })
            .catch(err => {
                console.error("Video loader registration error:", err);
                repurposerActiveVideoPath = "FAILED";
                checkRepurposeWorkshopReady(url);
            });
        });
    }

    function pollRepurposeVideoStatus(jobId, url) {
        const interval = setInterval(() => {
            fetch(`/api/video-status/${jobId}`)
                .then(res => res.json())
                .then(data => {
                    if (data.status === "SUCCESS") {
                        clearInterval(interval);
                        repurposerActiveVideoPath = data.result.video_path;
                        checkRepurposeWorkshopReady(url);
                    } else if (data.status === "FAILED") {
                        clearInterval(interval);
                        repurposerActiveVideoPath = "FAILED";
                        repurposerVideoFailureMessage = data.message || null;
                        checkRepurposeWorkshopReady(url);
                    }
                })
                .catch(err => {
                    clearInterval(interval);
                    repurposerActiveVideoPath = "FAILED";
                    checkRepurposeWorkshopReady(url);
                });
        }, 3000);
    }

    function checkRepurposeWorkshopReady(url) {
        if (repurposerScrapedData === null || repurposerActiveVideoPath === null) {
            if (repurposerScrapedData !== null) {
                document.getElementById("repurpose-loader-title").textContent = "Downloading Video File...";
                document.getElementById("repurpose-loader-status").textContent = "Running yt-dlp to scrape the mp4 binaries locally...";
            }
            return;
        }

        // Both ready! Reveal workspace
        repurposeGlobalLoader.classList.add("hidden");
        repurposeRunBtn.disabled = false;
        repurposeWorkspace.classList.remove("hidden");

        // Populate fields
        document.getElementById("repurpose-author-header").textContent = `@${repurposerScrapedData.author}`;
        document.getElementById("repurpose-platform-tag").textContent = "Social Video";
        
        const anchor = document.getElementById("repurpose-original-link-anchor");
        anchor.href = url;

        document.getElementById("repurpose-orig-post-text").value = repurposerScrapedData.original_post_text || "";
        
        document.getElementById("w-linkedin-text").value = repurposerScrapedData.repurposed_linkedin_post || "";
        document.getElementById("w-instagram-text").value = repurposerScrapedData.repurposed_instagram_caption || "";

        // Populate tweets
        const wTweetCardsContainer = document.getElementById("w-tweet-cards-container");
        wTweetCardsContainer.innerHTML = "";
        const tweets = repurposerScrapedData.repurposed_twitter_thread || ["Commentary thread"];
        
        tweets.forEach((tweetText, index) => {
            const card = document.createElement("div");
            card.className = "tweet-card";
            card.innerHTML = `
                <div class="tweet-header">
                     <span>Tweet ${index + 1}</span>
                     <span class="badge">Draft</span>
                </div>
                <textarea class="w-tweet-textarea" rows="4">${tweetText}</textarea>
                <div class="counter-container">
                     <span class="w-char-count">${tweetText.length}</span>/280
                </div>
            `;
            const textarea = card.querySelector(".w-tweet-textarea");
            const countSpan = card.querySelector(".w-char-count");
            const counterCont = card.querySelector(".counter-container");

            textarea.addEventListener("input", () => {
                const len = textarea.value.length;
                countSpan.textContent = len;
                if (len > 280) counterCont.classList.add("danger");
                else counterCont.classList.remove("danger");
            });

            wTweetCardsContainer.appendChild(card);
        });

        // Set video player
        const playerCont = document.getElementById("repurpose-video-player-container");
        const placeholder = document.getElementById("repurpose-video-placeholder");
        const player = document.getElementById("repurpose-video-player");

        if (repurposerActiveVideoPath && repurposerActiveVideoPath !== "FAILED") {
            placeholder.classList.add("hidden");
            playerCont.classList.remove("hidden");
            player.src = repurposerActiveVideoPath;
        } else {
            placeholder.classList.remove("hidden");
            placeholder.innerHTML = "";
            const p = document.createElement("p");
            p.style.cssText = "font-size:0.75rem; color:var(--text-secondary); padding: 2rem 0;";
            const failureText = repurposerVideoFailureMessage || "Video binary could not be parsed automatically.";
            p.textContent = `${failureText} Attributed copy is ready below.`;
            placeholder.appendChild(p);
            playerCont.classList.add("hidden");
            player.src = "";
        }
    }

    // Publish LinkedIn
    const publishWLinkedinBtn = document.getElementById("publish-w-linkedin-btn");
    if (publishWLinkedinBtn) {
        publishWLinkedinBtn.addEventListener("click", () => {
            const text = document.getElementById("w-linkedin-text").value;
            if (!text.trim()) return;

            publishWLinkedinBtn.disabled = true;
            publishWLinkedinBtn.textContent = "Publishing...";

            const video = (repurposerActiveVideoPath && repurposerActiveVideoPath !== "FAILED") ? repurposerActiveVideoPath : null;

            fetch("/api/publish/linkedin", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text: text, video_path: video })
            })
            .then(res => {
                if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to publish") });
                return res.json();
            })
            .then(data => {
                showToast(data.message || "Posted commentary to LinkedIn successfully!");
            })
            .catch(err => {
                showToast(err.message, true);
            })
            .finally(() => {
                publishWLinkedinBtn.disabled = false;
                publishWLinkedinBtn.textContent = "Publish";
            });
        });
    }

    // Publish Twitter
    const publishWTwitterBtn = document.getElementById("publish-w-twitter-btn");
    if (publishWTwitterBtn) {
        publishWTwitterBtn.addEventListener("click", () => {
            const textareas = document.querySelectorAll(".w-tweet-textarea");
            const thread = [];
            let hasError = false;
            
            textareas.forEach(ta => {
                if (ta.value.length > 280) hasError = true;
                thread.push(ta.value);
            });

            if (hasError) {
                showToast("One of your tweets exceeds 280 chars.", true);
                return;
            }

            publishWTwitterBtn.disabled = true;
            publishWTwitterBtn.textContent = "Publishing...";

            const video = (repurposerActiveVideoPath && repurposerActiveVideoPath !== "FAILED") ? repurposerActiveVideoPath : null;
            const payload = {
                is_thread: thread.length > 1,
                thread: thread,
                text: thread.length === 1 ? thread[0] : null,
                video_path: video
            };

            fetch("/api/publish/twitter", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            })
            .then(res => {
                if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to publish") });
                return res.json();
            })
            .then(data => {
                showToast(data.message || "Tweet thread published successfully!");
            })
            .catch(err => {
                showToast(err.message, true);
            })
            .finally(() => {
                publishWTwitterBtn.disabled = false;
                publishWTwitterBtn.textContent = "Publish Thread";
            });
        });
    }

    // Schedule post
    const repurposeScheduleBtn = document.getElementById("repurpose-schedule-btn");
    if (repurposeScheduleBtn) {
        repurposeScheduleBtn.addEventListener("click", () => {
            const timeVal = document.getElementById("repurpose-schedule-time").value;
            if (!timeVal) {
                showToast("Select date & time to schedule.", true);
                return;
            }

            const platform = document.getElementById("repurpose-schedule-platform").value;
            const author = document.getElementById("repurpose-author-header").textContent;
            const campaignTitle = `Repurposed: ${author}`;

            const text = document.getElementById("w-linkedin-text").value;
            const textareas = document.querySelectorAll(".w-tweet-textarea");
            const thread = [];
            textareas.forEach(ta => thread.push(ta.value));

            const video = (repurposerActiveVideoPath && repurposerActiveVideoPath !== "FAILED") ? repurposerActiveVideoPath : null;

            repurposeScheduleBtn.disabled = true;
            repurposeScheduleBtn.textContent = "Scheduling...";

            const payload = {
                platform: platform,
                text: text,
                thread: thread.length > 0 ? thread : null,
                scheduled_time: timeVal,
                campaign_title: campaignTitle,
                video_path: video
            };

            fetch("/api/schedule-post", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            })
            .then(res => {
                if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to schedule") });
                return res.json();
            })
            .then(data => {
                showToast("Post scheduled successfully!");
                document.getElementById("repurpose-schedule-time").value = "";
                fetchScheduledQueue();
            })
            .catch(err => {
                showToast(err.message, true);
            })
            .finally(() => {
                repurposeScheduleBtn.disabled = false;
                repurposeScheduleBtn.textContent = "Schedule Release";
            });
        });
    }

    // Copy X Thread
    const copyWTwitterBtn = document.getElementById("copy-w-twitter-btn");
    if (copyWTwitterBtn) {
        copyWTwitterBtn.addEventListener("click", () => {
            const textareas = document.querySelectorAll(".w-tweet-textarea");
            let fullThreadText = "";
            textareas.forEach((ta, idx) => {
                fullThreadText += `[Tweet ${idx + 1}]\n${ta.value}\n\n`;
            });
            
            navigator.clipboard.writeText(fullThreadText.trim())
                .then(() => showToast("Copied full repurposed Twitter thread!"))
                .catch(err => showToast("Failed to copy.", true));
        });
    }

    /* ==========================================================================
       GROWTH OS COMMAND CENTER
       ========================================================================== */
    let growthState = null;
    let growthPollTimer = null;
    const byId = (id) => document.getElementById(id);
    const growthList = (id, items, renderer, emptyText = "No items yet.") => {
        const el = byId(id);
        if (!el) return;
        if (!items || items.length === 0) {
            el.innerHTML = `<div class="growth-empty">${escapeHtml(emptyText)}</div>`;
            return;
        }
        el.innerHTML = items.map(renderer).join("");
    };
    const shortDate = (val) => {
        if (!val) return "";
        try {
            return new Date(val).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
        } catch {
            return String(val).slice(0, 16);
        }
    };
    const postGrowthItem = (collection, item) => {
        return fetch("/api/growth-os/item", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ collection, item })
        }).then(res => {
            if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Growth OS update failed") });
            return res.json();
        });
    };

    function renderGrowthOS(data) {
        growthState = data;
        if (byId("growth-stat-inbox")) byId("growth-stat-inbox").textContent = (data.unified_inbox || []).length;
        if (byId("growth-stat-calendar")) byId("growth-stat-calendar").textContent = (data.calendar || []).length;
        if (byId("growth-stat-assets")) byId("growth-stat-assets").textContent = (data.assets || []).length;
        if (byId("growth-stat-besttime")) byId("growth-stat-besttime").textContent = data.best_time?.hour !== null && data.best_time?.hour !== undefined ? `${data.best_time.hour}:00` : "--";

        growthList("growth-inbox-list", data.unified_inbox, item => `
            <div class="growth-list-item">
                <strong>${escapeHtml(item.platform || item.source_platform || "Inbox")} · @${escapeHtml(item.source_author || "unknown")}</strong>
                <p>${escapeHtml(item.source_text || "")}</p>
                <span>${escapeHtml(item.drafted_reply || "AI reply will appear after refresh.")}</span>
            </div>`, "No active replies or DMs queued.");

        growthList("growth-calendar-list", data.calendar, item => `
            <div class="growth-list-item">
                <strong>${escapeHtml(item.title || "Scheduled post")}</strong>
                <span>${escapeHtml(item.platform || "platform")} · ${escapeHtml(item.status || "draft")} · ${escapeHtml(shortDate(item.scheduled_time))}</span>
            </div>`, "No calendar items yet.");

        growthList("growth-asset-list", data.assets, item => {
            const media = item.type === "image"
                ? `<img class="growth-thumb" src="${escapeHtml(item.url)}" alt="">`
                : `<video class="growth-thumb" src="${escapeHtml(item.url)}" muted></video>`;
            return `<a class="growth-list-item" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">
                ${media}
                <span><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.type)} · ${Math.round((item.size || 0) / 1024)} KB</span></span>
            </a>`;
        }, "Generated videos and images will appear here.");

        const listeningRows = [
            ...((data.listening_topics || []).map(item => ({ kind: "topic", ...item }))),
            ...((data.listening_signals || []).map(item => ({ kind: "signal", ...item })))
        ];
        growthList("growth-listening-list", listeningRows, item => item.kind === "signal" ? `
            <div class="growth-list-item"><strong>${escapeHtml(item.topic || item.platform || "Live signal")}</strong><span>${escapeHtml(item.platform || "")} · ${escapeHtml(item.metric || "")}</span><p>${escapeHtml(item.summary || "")}</p>${item.url ? `<a class="growth-link" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">Open source</a>` : ""}</div>` : `
            <div class="growth-list-item"><strong>${escapeHtml(item.keyword)}</strong><span>${escapeHtml(item.type || "keyword")} · ${escapeHtml(item.priority || "medium")} · ${escapeHtml(item.status || "active")}</span></div>`, "Add a topic, then run live refresh.");

        growthList("growth-competitor-list", data.competitors, item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.name || item.handle)}</strong><span>${escapeHtml(item.handle || "")} · ${escapeHtml(item.posting_frequency || "frequency TBD")} · ${escapeHtml(item.engagement_velocity || "velocity TBD")}</span><p>${escapeHtml((item.format_patterns || []).join(", "))}</p></div>`);

        growthList("growth-evergreen-list", data.evergreen_buckets, item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.cadence)} · recycle every ${escapeHtml(item.recycle_days)} days · ${(item.items || []).length} items</span></div>`);

        const brand = data.brand_kit || {};
        if (byId("growth-brand-colors")) byId("growth-brand-colors").value = (brand.colors || []).join(", ");
        if (byId("growth-brand-logo")) byId("growth-brand-logo").value = brand.logo_url || "";
        if (byId("growth-brand-tone")) byId("growth-brand-tone").value = (brand.tone_presets || []).join(", ");
        growthList("growth-brand-rules", Object.entries(brand.template_rules || {}).map(([name, rule]) => ({ name, rule })), item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.rule)}</span></div>`, "No template rules yet.");

        growthList("growth-abtest-list", data.ab_tests, item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.metric || "engagement")} · ${escapeHtml(item.status || "draft")}</span><p>${escapeHtml((item.variants || []).join(" vs "))}</p></div>`);

        growthList("growth-crm-list", data.crm_contacts, item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.name || item.handle)}</strong><span>${escapeHtml(item.handle || "")} · ${escapeHtml((item.labels || []).join(", "))}</span><p>${escapeHtml(item.suggested_followup || item.notes || "")}</p></div>`);

        growthList("growth-pipeline-list", (data.campaign_plan || []).slice(0, 8), item => `
            <div class="growth-list-item"><strong>Day ${escapeHtml(item.day)} · ${escapeHtml(item.platform)} · ${escapeHtml(item.theme)}</strong><span>${escapeHtml(item.date)} · ${escapeHtml(item.asset_type)} · ${escapeHtml(item.template)}</span><p>${escapeHtml(item.hook)}</p></div>`, "Generate a campaign plan to fill the pipeline.");

        const bio = data.link_in_bio || {};
        if (byId("growth-bio-slug")) byId("growth-bio-slug").value = bio.slug || "6frame";
        if (byId("growth-bio-headline")) byId("growth-bio-headline").value = bio.headline || "";
        if (byId("growth-bio-video")) byId("growth-bio-video").value = bio.featured_video || "";
        if (byId("growth-bio-link")) byId("growth-bio-link").href = `/b/${encodeURIComponent(bio.slug || "6frame")}`;

        growthList("growth-utm-list", data.utm_campaigns, item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.campaign)}</strong><a class="growth-link" href="${escapeHtml(item.tracked_url || item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.tracked_url || item.url)}</a><span>${escapeHtml(item.source)} · ${escapeHtml(item.medium)} · ${escapeHtml(item.clicks || 0)} clicks</span></div>`, "Build a UTM link to start tracking.");

        growthList("growth-workspace-list", data.workspaces, item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.role || "client")} · ${escapeHtml(item.id)}</span></div>`);

        growthList("growth-rules-list", data.automation_rules, item => `
            <div class="growth-list-item"><strong>If ${escapeHtml(item.trigger)}</strong><span>${escapeHtml(item.condition)} → ${escapeHtml(item.action)} · ${item.enabled === false ? "off" : "on"}</span></div>`);
        growthList("growth-events-list", (data.automation_events || []).slice(0, 6), item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.status || "EVENT")} · ${escapeHtml(item.trigger || "rule")}</strong><span>${escapeHtml(shortDate(item.created_at))}</span><p>${escapeHtml(item.message || "")}</p></div>`, "No automation runs yet.");

        growthList("growth-integrations-list", data.integrations, item => `
            <div class="growth-list-item"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.status || "ready")}</span><p>${escapeHtml(item.webhook_url || "Webhook/connector slot ready.")}</p></div>`);

        const reports = data.report_history && data.report_history.length ? data.report_history : [data.reports_preview].filter(Boolean);
        growthList("growth-report-list", reports, item => `
            <div class="growth-list-item"><strong>Report · ${escapeHtml(shortDate(item.created_at))}</strong><span>${escapeHtml(item.summary?.published || 0)} published · ${escapeHtml(item.summary?.awaiting_approval || 0)} awaiting · best hour ${escapeHtml(item.summary?.best_hour ?? "--")}</span><p>${escapeHtml((item.recommendations || []).slice(0, 2).join(" "))}</p>${item.pdf_path ? `<a class="growth-link" href="${escapeHtml(item.pdf_path)}" target="_blank" rel="noreferrer">Open PDF</a>` : ""}${item.html_path ? `<a class="growth-link" href="${escapeHtml(item.html_path)}" target="_blank" rel="noreferrer">Open HTML</a>` : ""}</div>`);
    }

    function fetchGrowthOS() {
        if (!byId("growth-view")) return;
        fetch("/api/growth-os")
            .then(res => {
                if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Failed to load Growth OS") });
                return res.json();
            })
            .then(renderGrowthOS)
            .catch(err => showToast(err.message, true));
    }

    function attachGrowthHandlers() {
        const click = (id, handler) => {
            const el = byId(id);
            if (el) el.addEventListener("click", handler);
        };
        const engineDurations = {
            fal_hailuo_02: [10],
            fal_hailuo_23: [10],
            fal_seedance_fast: [8, 10, 12],
            fal_ltx_fast: [8, 10, 12, 14, 16, 18, 20],
            google_veo: [8],
            google_veo_lite: [8],
            google_veo_fast: [8]
        };
        const updateGrowthDurations = () => {
            const engine = byId("growth-video-engine")?.value || "fal_hailuo_02";
            const durationSelect = byId("growth-video-duration");
            if (!durationSelect) return;
            const previous = durationSelect.value;
            const values = engineDurations[engine] || [8, 10];
            durationSelect.innerHTML = values.map(v => `<option value="${v}">${v}s each</option>`).join("");
            durationSelect.value = values.includes(parseInt(previous, 10)) ? previous : String(values[values.length - 1]);
        };
        const engineSelect = byId("growth-video-engine");
        if (engineSelect) {
            engineSelect.addEventListener("change", updateGrowthDurations);
            updateGrowthDurations();
        }
        click("growth-refresh-btn", () => {
            const btn = byId("growth-refresh-btn");
            if (btn) {
                btn.disabled = true;
                btn.textContent = "Refreshing...";
            }
            fetch("/api/growth-os/live-refresh", { method: "POST" })
                .then(res => {
                    if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Live refresh failed") });
                    return res.json();
                })
                .then(() => {
                    showToast("Live Growth OS refresh started.");
                    setTimeout(fetchGrowthOS, 2500);
                })
                .catch(err => showToast(err.message, true))
                .finally(() => {
                    if (btn) {
                        btn.disabled = false;
                        btn.textContent = "Refresh";
                    }
                });
        });
        click("growth-report-btn", () => {
            fetch("/api/growth-os/report", { method: "POST" })
                .then(res => res.json())
                .then(() => { showToast("Growth report built."); fetchGrowthOS(); })
                .catch(err => showToast(err.message, true));
        });
        click("growth-plan-btn", () => {
            fetch("/api/growth-os/campaign-plan", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    business_url: byId("growth-plan-url")?.value || "",
                    goals: byId("growth-plan-goals")?.value || "",
                    audience: byId("growth-plan-audience")?.value || ""
                })
            })
            .then(res => res.json())
            .then(() => { showToast("30-day campaign plan generated."); fetchGrowthOS(); })
            .catch(err => showToast(err.message, true));
        });
        click("growth-add-topic-btn", () => {
            const keyword = byId("growth-topic-input")?.value.trim();
            if (!keyword) return showToast("Add a topic first.", true);
            postGrowthItem("listening_topics", { keyword, type: keyword.startsWith("#") ? "hashtag" : "keyword", priority: "medium", status: "active" })
                .then(() => { byId("growth-topic-input").value = ""; fetchGrowthOS(); });
        });
        click("growth-add-competitor-btn", () => {
            const name = byId("growth-competitor-input")?.value.trim();
            if (!name) return showToast("Add a competitor first.", true);
            postGrowthItem("competitors", { name, handle: name.startsWith("@") ? name : "", platform: "social", posting_frequency: "tracking", format_patterns: ["top posts", "hooks", "format cadence"], engagement_velocity: "learning" })
                .then(() => { byId("growth-competitor-input").value = ""; fetchGrowthOS(); });
        });
        click("growth-brand-save-btn", () => {
            fetch("/api/growth-os/brand_kit", {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ data: {
                    colors: (byId("growth-brand-colors")?.value || "").split(",").map(v => v.trim()).filter(Boolean),
                    logo_url: byId("growth-brand-logo")?.value || "",
                    tone_presets: (byId("growth-brand-tone")?.value || "").split(",").map(v => v.trim()).filter(Boolean)
                }})
            }).then(() => { showToast("Brand kit saved."); fetchGrowthOS(); });
        });
        click("growth-add-abtest-btn", () => {
            const name = byId("growth-abtest-input")?.value.trim();
            if (!name) return showToast("Name the test first.", true);
            postGrowthItem("ab_tests", { name, variants: ["hook A", "hook B"], metric: "engagement_score", status: "draft" })
                .then(() => { byId("growth-abtest-input").value = ""; fetchGrowthOS(); });
        });
        click("growth-add-crm-btn", () => {
            const handle = byId("growth-crm-input")?.value.trim();
            if (!handle) return showToast("Add a contact first.", true);
            postGrowthItem("crm_contacts", { name: handle, handle, platform: "social", labels: ["lead"], notes: "Added from Growth OS.", suggested_followup: "Review latest interaction and draft a follow-up." })
                .then(() => { byId("growth-crm-input").value = ""; fetchGrowthOS(); });
        });
        click("growth-bio-save-btn", () => {
            fetch("/api/growth-os/link_in_bio", {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ data: {
                    slug: byId("growth-bio-slug")?.value || "6frame",
                    headline: byId("growth-bio-headline")?.value || "6Frame Studio",
                    featured_video: byId("growth-bio-video")?.value || ""
                }})
            }).then(() => { showToast("Link-in-bio page saved."); fetchGrowthOS(); });
        });
        click("growth-utm-btn", () => {
            const baseUrl = byId("growth-utm-url")?.value.trim();
            if (!baseUrl) return showToast("Add a destination URL first.", true);
            fetch("/api/growth-os/utm", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    base_url: baseUrl,
                    campaign: byId("growth-utm-campaign")?.value || "6frame",
                    content: byId("growth-utm-content")?.value || "",
                    source: "social",
                    medium: "organic"
                })
            }).then(() => { showToast("UTM link created."); fetchGrowthOS(); });
        });
        click("growth-add-workspace-btn", () => {
            const name = byId("growth-workspace-input")?.value.trim();
            if (!name) return showToast("Add a workspace name first.", true);
            postGrowthItem("workspaces", { name, role: "client" })
                .then(() => { byId("growth-workspace-input").value = ""; fetchGrowthOS(); });
        });
        click("growth-add-rule-btn", () => {
            const trigger = byId("growth-rule-trigger")?.value.trim();
            const condition = byId("growth-rule-condition")?.value.trim();
            const action = byId("growth-rule-action")?.value.trim();
            if (!trigger || !action) return showToast("Add trigger and action first.", true);
            postGrowthItem("automation_rules", { trigger, condition: condition || "any", action, enabled: true })
                .then(() => {
                    byId("growth-rule-trigger").value = "";
                    byId("growth-rule-condition").value = "";
                    byId("growth-rule-action").value = "";
                    fetchGrowthOS();
                });
        });
        click("growth-run-rules-btn", () => {
            const btn = byId("growth-run-rules-btn");
            if (btn) {
                btn.disabled = true;
                btn.textContent = "Running...";
            }
            fetch("/api/growth-os/run-automation-rules", { method: "POST" })
                .then(res => {
                    if (!res.ok) return res.json().then(e => { throw new Error(e.detail || "Automation run failed") });
                    return res.json();
                })
                .then(data => {
                    showToast(`Automation run complete: ${(data.events || []).length} event(s).`);
                    fetchGrowthOS();
                })
                .catch(err => showToast(err.message, true))
                .finally(() => {
                    if (btn) {
                        btn.disabled = false;
                        btn.textContent = "Run";
                    }
                });
        });
        click("growth-multiscene-btn", () => {
            const prompt = byId("growth-video-prompt")?.value.trim();
            if (!prompt) return showToast("Add a video prompt first.", true);
            const btn = byId("growth-multiscene-btn");
            const status = byId("growth-multiscene-status");
            btn.disabled = true;
            status.textContent = "Queued...";
            fetch("/api/growth-os/multiscene-video", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    prompt,
                    engine: byId("growth-video-engine")?.value || "fal_hailuo_02",
                    scene_count: parseInt(byId("growth-video-scenes")?.value || "3", 10),
                    scene_duration: parseInt(byId("growth-video-duration")?.value || "6", 10),
                    template_id: byId("growth-video-template")?.value || "hook_burst",
                    apply_template: true,
                    title: "Multi-Scene Campaign",
                    subtitle: "Generated by 6Frame Studio Growth OS"
                })
            })
            .then(res => res.json())
            .then(data => pollGrowthVideo(data.job_id, btn))
            .catch(err => {
                btn.disabled = false;
                status.textContent = "Render failed to start.";
                showToast(err.message, true);
            });
        });
    }

    function pollGrowthVideo(jobId, btn) {
        if (growthPollTimer) clearInterval(growthPollTimer);
        const status = byId("growth-multiscene-status");
        const output = byId("growth-multiscene-output");
        growthPollTimer = setInterval(() => {
            fetch(`/api/video-status/${jobId}`)
                .then(res => res.json())
                .then(data => {
                    status.textContent = `${data.status || "PROCESSING"} · ${data.progress || 0}% · ${data.message || ""}`;
                    if (data.status === "SUCCESS") {
                        clearInterval(growthPollTimer);
                        btn.disabled = false;
                        const videoPath = data.result?.video_path;
                        output.innerHTML = videoPath ? `<video src="${escapeHtml(videoPath)}" controls></video><a class="growth-link" href="${escapeHtml(videoPath)}" target="_blank" rel="noreferrer">Open rendered video</a>` : "";
                        fetchGrowthOS();
                    }
                    if (data.status === "FAILED") {
                        clearInterval(growthPollTimer);
                        btn.disabled = false;
                        showToast(data.message || "Multi-scene render failed.", true);
                    }
                })
                .catch(err => {
                    clearInterval(growthPollTimer);
                    btn.disabled = false;
                    showToast(err.message, true);
                });
        }, 4000);
    }

    attachGrowthHandlers();

    // Initial load and polling setup
    fetchScheduledQueue();
    fetchApprovalQueue();
    fetchAnalyticsSummary();
    fetchEngagementQueue();
    fetchGrowthOS();
    setInterval(fetchScheduledQueue, 10000);
    setInterval(fetchApprovalQueue, 10000);
    setInterval(fetchAnalyticsSummary, 30000);
    setInterval(fetchEngagementQueue, 30000);
});
