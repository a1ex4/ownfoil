'use strict';

// Global helper for Overrides functionality.
// Attaches as window.Ownfoil.Overrides and requires jQuery.

((global, $) => {
  if (!$) {
    console.warn('Ownfoil Overrides requires jQuery.');
    return;
  }

  const namespace = global.Ownfoil = global.Ownfoil || {};
  const PLACEHOLDER_TEXT = () => window.PLACEHOLDER_TEXT || "Image Unavailable";
  const DEFAULT_BANNER = () => window.DEFAULT_BANNER || `https://placehold.co/400x225/png?text=${encodeURIComponent(PLACEHOLDER_TEXT())}`;
  const DEFAULT_ICON   = () => window.DEFAULT_ICON   || `https://placehold.co/400x400/png?text=${encodeURIComponent(PLACEHOLDER_TEXT())}`;

  // Forces browsers to refetch updated artwork after saves/resets
  const ARTWORK_BUSTERS = new Map(); // app_id -> integer
  const getBuster = (appId) => ARTWORK_BUSTERS.get(appId) || 0;
  const bumpBuster = (appId) => ARTWORK_BUSTERS.set(appId, getBuster(appId) + 1);
  const hex16 = (s) => /^[0-9A-F]{16}$/i.test((s || '').toString().trim());

  // --- Overrides state ---
  // key: app_id  -> override object
  const overridesByKey = new Map();

  // --- Redirects state ---
  // key: app_id -> { corrected_title_id, projection }
  const redirectsByAppId = new Map();

  // external environment (supplied by index.html)
  const env = {
    getGames: null,      // () => games array
    applyFilters: null   // () => void
  };

  const $overrideModalEl = $('#overrideEditorModal');
  const overrideModal = () => {
    const el = $overrideModalEl.get(0);
    if (!el) return null;
    return bootstrap.Modal.getOrCreateInstance(el);
  };

  // ----------------- Helpers -----------------
  const _trimmedOrNull = (u) => (typeof u === 'string' && u.trim().length) ? u.trim() : null;
  const trimOrNull = (v) => _trimmedOrNull((v ?? '').toString());
  const numOrNull = (v) => {
    const t = trimOrNull(v);
    if (t === null) return null;
    const n = Number(t);
    return Number.isFinite(n) ? n : null;
  };
  const addBuster = (url, buster = 0) =>
    !url || (/^(data:|blob:)/i.test(url)) || !buster
    ? url
    : `${url}${url.includes('?') ? '&' : '?'}b=${buster}`;
  const appKey = (gameOrOverride) => gameOrOverride?.app_id || '';

  // Derive Version for the modal:
  // - BASE  -> latest OWNED update version (fallback: latest available; fallback: app_version)
  // - DLC   -> app_version of the DLC itself
  const deriveTitleDbVersion = (game) => {
    if (!game) return null;

    const parseNum = (x) => {
      if (typeof x === 'number' && Number.isFinite(x)) return x;
      if (typeof x === 'string' && /^\d+$/.test(x))   return Number(x);
      return null;
    };

    const type = (game.app_type || '').toUpperCase();

    if (type === 'DLC') {
      // DLCs: show the app's own version (app_version)
      return parseNum(game.app_version);
    }

    // BASE (or anything else): prefer latest OWNED update version
    const updates = Array.isArray(game.version) ? game.version : [];

    const pickMaxVersion = (arr) => {
      let max = null;
      for (const item of arr) {
        const n = parseNum(item?.version);
        if (n !== null && (max === null || n > max)) max = n;
      }
      return max;
    };

    if (updates.length) {
      const owned = updates.filter(u => u && u.owned === true);
      const maxOwned = owned.length ? pickMaxVersion(owned) : null;
      if (maxOwned !== null) return maxOwned;

      const maxAny = pickMaxVersion(updates);
      if (maxAny !== null) return maxAny;
    }

    // No updates? fall back to the app's own version (often 0 for v0 base)
    return parseNum(game.app_version);
  };

  // Recognition flags (stable against overrides)
  const computeRecognitionFlags = (game) => {
    if (!game || typeof game !== 'object') return { isUnrecognized: true, hasTitleDb: false };

    const origName = (game?._orig?.title_id_name ?? '').trim();
    const curName  = (game?.title_id_name ?? game?.name ?? '').trim();
    const anyName  = origName || curName;

    // Prefer server-provided boolean if available
    const explicit =
      (typeof game.has_title_db === 'boolean') ? game.has_title_db :
      (typeof game.hasTitleDb  === 'boolean') ? game.hasTitleDb  :
      null;

      // Heuristic name check (treat literal "Unrecognized"/"Unidentified" as unrecognized)
    const looksNamed = !!anyName && !/^(unrecognized|unidentified)$/i.test(anyName);

    // A plausible 16-hex TitleID anywhere we usually carry it
    const idGuess = (game.app_id ?? game.title_id ?? game.id ?? '').toString().trim();
    const hasHexId = hex16(idGuess);

    // Decision: explicit boolean wins; otherwise require both a real-looking name and a 16-hex id
    const hasTitleDb = (explicit !== null) ? explicit : (looksNamed && hasHexId);

    return { isUnrecognized: !hasTitleDb, hasTitleDb };
  };

  const isUnrecognizedGame = (game) => {
    if (!game) return false;
    if (typeof game.isUnrecognized === 'boolean' && typeof game.hasTitleDb === 'boolean') {
      return game.isUnrecognized || !game.hasTitleDb;
    }
    const flags = computeRecognitionFlags(game);
    game.isUnrecognized = flags.isUnrecognized; // cache
    game.hasTitleDb     = flags.hasTitleDb;     // cache
    return flags.isUnrecognized;
  };

  // Find the base game for a DLC by TitleID prefix (first 12 hex chars are shared).
  const findBaseForDlc = (dlcGame, allGames) => {
    if (!dlcGame || !Array.isArray(allGames)) return null;
    if ((dlcGame.app_type || '').toUpperCase() !== 'DLC') return null;

    const appId = (dlcGame.app_id || '').toUpperCase();
    if (!hex16(appId)) return null;

    const familyPrefix = appId.slice(0, 12); // e.g. 010056E00853
    const base = allGames.find(g =>
      (g.app_type || '').toUpperCase() === 'BASE' &&
      typeof g.app_id === 'string' &&
      g.app_id.toUpperCase().startsWith(familyPrefix)
    );
    return base || null;
  };

  // return the correct display title for the big card header.
  const displayTitleFor = (game, allGames) => {
    const type = (game?.app_type || '').toUpperCase();
    if (type === 'DLC') {
      // Always show the BASE title for DLC cards
      const base = findBaseForDlc(game, allGames);
      if (base) return (base.name || base.title_id_name || 'Unrecognized');
    }
    // For BASE (and anything else), use TitleDB name then fallback
    return (game?.name || game?.title_id_name || 'Unrecognized');
  };
  
  const pickTidForDisplay = (game, ovr) => {
    const candidates = [
      ovr?.corrected_title_id,     // explicit override first
      game?.corrected_title_id,    // server-computed corrected id if present
      game?.app_id,                // app-specific id (BASE or DLC)
      game?.dlc_title_id,          // distinct DLC id if existing
      game?.title_id,              // family/base id (fallback)
      game?.id                     // last resort
    ];
    for (const c of candidates) {
      const t = (c || '').toString().trim();
      if (hex16(t)) return t.toUpperCase();
    }
    return '';
  };

  const pickNameForEdit = (game, ovr) => (ovr && typeof ovr.name === 'string' && ovr.name.trim())
    ? ovr.name.trim()
    : (game?.name || game?.title_id_name || '').trim();

  // ----------------- Overlay helpers -----------------
  // Apply (or remove) a single override onto matching games in memory.
  const applyOverrideToGamesByKey = (key, games) => {
    if (!key || !Array.isArray(games)) return;
    const ovr = overridesByKey.get(key) || null;
    const affecteds = games.filter(g => appKey(g) === key);

    affecteds.forEach(g => {
      if (!g._orig)  g._orig = { name: g.name, title_id_name: g.title_id_name, release_date: g.release_date ?? null, app_type: g.app_type };
      const type = (g.app_type || '').toUpperCase();

      if (ovr && ovr.enabled) {
        // --- NAME override ---
        if (ovr.name && typeof ovr.name === 'string' && ovr.name.trim().length) {
          const ovrName = ovr.name.trim();
          g.name = ovrName;
          if (type !== 'DLC')
            g.title_id_name = ovrName;
        } else {
          // No explicit name override: do not touch g.name/title_id_name.
          // This preserves any redirect projection already applied.
        }

        // apply release_date if present (allow clearing with null)
        if (ovr.release_date && typeof ovr.release_date === 'string' && ovr.release_date.trim().length) {
          g.release_date = ovr.release_date.trim();
        }
      } else {
        // Override disabled/absent -> restore originals
        if (g._orig) {
          g.title_id_name = g._orig.title_id_name;
          g.name = g._orig.name;
          g.release_date = g._orig.release_date ?? null;
        }
      }
    });
  };

  const reapplyAllOverridesToGames = (games) => {
    if (!Array.isArray(games)) return;
    const keys = new Set();
    games.forEach(g => keys.add(appKey(g)));
    keys.forEach(k => applyOverrideToGamesByKey(k, games));
  }

  // Redirect helpers
  const getRedirectForApp = (appId) => {
    const k = (appId || '').trim();
    return k ? (redirectsByAppId.get(k) || null) : null;
  };

  // Overlay a redirect projection onto a game (identifiers unchanged).
  // Mutates the game object for render-time fields only; does NOT touch g._orig.
  const applyRedirectToGame = (game) => {
    if (!game || !game.app_id) return game;
    const r = getRedirectForApp(game.app_id);
    if (!r || !r.projection) return game;

    const proj = r.projection;

    // Mark correction context (useful for badges/logic)
    game.corrected_title_id = r.corrected_title_id || game.corrected_title_id || null;
    game.recognized_via_correction = true;

    // Overlay display metadata (leave identifiers alone)
    if (typeof proj.name === 'string')        game.name = proj.name;
    if (typeof proj.description === 'string') game.description = proj.description;
    if (typeof proj.region === 'string')      game.region = proj.region;
    if (typeof proj.release_date === 'string') game.release_date = proj.release_date;

    if (proj.bannerUrl) game.bannerUrl = proj.bannerUrl;
    if (proj.iconUrl)   game.iconUrl   = proj.iconUrl;
    if (Array.isArray(proj.category))  game.category = proj.category.slice();

    return game;
  };

  // Apply redirects to an array of games (in-place overlay for render-time fields)
  const applyRedirectsToGames = (gamesArray) => {
    if (!Array.isArray(gamesArray) || redirectsByAppId.size === 0) return;
    for (const g of gamesArray) applyRedirectToGame(g);
  };

  // ----------------- Fetching -----------------
    const fetchOverrides = async () => {
      try {
        const list = await $.ajax({
          url: '/api/overrides',
          method: 'GET',
          dataType: 'json',
          ifModified: true
        });

        // If 304, jQuery resolves but `list` can be undefined/null → no change
        if (!list) return { overridesChanged: false, redirectsChanged: false };

        overridesByKey.clear();
        (Array.isArray(list.items) ? list.items : []).forEach(o => {
          const k = appKey(o);
          if (k) overridesByKey.set(k, o);
        });

        // load redirects
        redirectsByAppId.clear();
        const r = list && list.redirects && typeof list.redirects === 'object' ? list.redirects : null;
        if (r) {
          Object.entries(r).forEach(([appId, val]) => {
            if (!appId) return;
            if (val && (typeof val === 'object') && (val.corrected_title_id || val.projection)) {
              redirectsByAppId.set(appId, {
                corrected_title_id: val.corrected_title_id || null,
                projection: (val.projection && typeof val.projection === 'object') ? val.projection : null
              });
            }
          });
        }

        // if some other view uses reapply immediately:
        if (env.getGames) reapplyAllOverridesToGames(env.getGames());
        
        return { overridesChanged: true, redirectsChanged: true };
      } catch (e) {
        overridesByKey.clear();
        redirectsByAppId.clear();
      }
    };


  // ----------------- Derived artwork URLs -----------------
  const bannerUrlFor = (game) => {
    const ovr = getOverrideForGame(game);
    const ovrUrl = _trimmedOrNull(ovr?.banner_path) || _trimmedOrNull(ovr?.bannerUrl);
    if (ovrUrl) return addBuster(ovrUrl, getBuster(game.app_id));

    return (
      _trimmedOrNull(game.banner_path) || _trimmedOrNull(game.bannerUrl) || _trimmedOrNull(game.banner) ||
      _trimmedOrNull(game.iconUrl) || DEFAULT_BANNER()
    );
  }

  const iconUrlFor = (game) =>{
    const ovr = getOverrideForGame(game);
    const ovrUrl = _trimmedOrNull(ovr?.icon_path) || _trimmedOrNull(ovr?.iconUrl);
    if (ovrUrl) return addBuster(ovrUrl, getBuster(game.app_id));

    return (
      _trimmedOrNull(game.iconUrl) || _trimmedOrNull(game.icon) ||
      _trimmedOrNull(game.banner_path) || _trimmedOrNull(game.bannerUrl) || _trimmedOrNull(game.banner) ||
      DEFAULT_ICON()
    );
  }

  const getOverrideForGame = (game) => { const k = appKey(game); return k ? overridesByKey.get(k) : null; }

  const hasActiveOverride = (game) => { const o = getOverrideForGame(game); return !!(o && o.enabled !== false); }

  // ----------------- Cropping helpers -----------------
  const cropBannerFileToDataURL = (file, callback) => {
    const TARGET_W = 400, TARGET_H = 225;
    const img = new Image();
    const url = URL.createObjectURL(file);

    img.onload = () => {
      try {
        const canvas = document.createElement('canvas');
        canvas.width = TARGET_W;
        canvas.height = TARGET_H;
        const ctx = canvas.getContext('2d');

        const srcW = img.naturalWidth || img.width;
        const srcH = img.naturalHeight || img.height;
        if (!srcW || !srcH) { URL.revokeObjectURL(url); return callback(null); }

        const scale = Math.max(TARGET_W / srcW, TARGET_H / srcH);
        const drawW = Math.round(srcW * scale);
        const drawH = Math.round(srcH * scale);
        const dx = Math.round((TARGET_W - drawW) / 2);
        const dy = Math.round((TARGET_H - drawH) / 2);

        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.clearRect(0, 0, TARGET_W, TARGET_H);
        ctx.drawImage(img, dx, dy, drawW, drawH);

        const dataURL = canvas.toDataURL('image/png');
        URL.revokeObjectURL(url);
        callback(dataURL);
      } catch (e) {
        URL.revokeObjectURL(url);
        callback(null);
      }
    };

    img.onerror = () => { URL.revokeObjectURL(url); callback(null); };
    img.src = url;
  }

  const cropIconFileToDataURL = (file, callback) => {
    const TARGET = 400;
    const img = new Image();
    const url = URL.createObjectURL(file);

    img.onload = () => {
      try {
        const canvas = document.createElement('canvas');
        canvas.width = TARGET; canvas.height = TARGET;
        const ctx = canvas.getContext('2d');

        const srcW = img.naturalWidth || img.width;
        const srcH = img.naturalHeight || img.height;
        if (!srcW || !srcH) { URL.revokeObjectURL(url); return callback(null); }

        const scale = Math.max(TARGET / srcW, TARGET / srcH);
        const drawW = Math.round(srcW * scale);
        const drawH = Math.round(srcH * scale);
        const dx = Math.round((TARGET - drawW) / 2);
        const dy = Math.round((TARGET - drawH) / 2);

        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.clearRect(0, 0, TARGET, TARGET);
        ctx.drawImage(img, dx, dy, drawW, drawH);

        const dataURL = canvas.toDataURL('image/png');
        URL.revokeObjectURL(url);
        callback(dataURL);
      } catch (e) {
        URL.revokeObjectURL(url);
        callback(null);
      }
    };

    img.onerror = () => { URL.revokeObjectURL(url); callback(null); };
    img.src = url;
  }

  // ----------------- Modal open / Save / Reset -----------------
  const openOverrideEditor = (game) => {
    if (!window.IS_ADMIN) return;
    const k = appKey(game);
    const ovr = k ? overridesByKey.get(k) : null;

    $('#ovr-id').val(ovr?.id || '');
    $('#ovr-app-id').val(game.app_id || '');
    $('#ovr-file-name').text(game.file_basename || '');

    // --- TitleDB baselines for the 4 fields ---
    const tdReleaseDate = trimOrNull(game.release_date);
    const tdRegion      = trimOrNull(game.region);
    const tdDescription = trimOrNull(game.description);
    const tdVersionNum  = deriveTitleDbVersion(game); // number or null

    // Name
    $('#ovr-name').val(pickNameForEdit(game, ovr));
    $('#ovr-name')
      .data('origName', ovr?.name ?? (game.title_id_name || game.name || ''))
      .data('everEdited', false);

    // Region
    const initialRegion = (ovr?.region != null) ? trimOrNull(ovr.region) : tdRegion;
    $('#ovr-region')
      .val(initialRegion ?? '')
      .data('origVal', initialRegion ?? '')
      .data('everEdited', false);

    // Release date
    const initialReleaseDate = (ovr?.release_date != null) ? trimOrNull(ovr.release_date) : tdReleaseDate;
    $('#ov-release-date')
      .val(initialReleaseDate ?? '')
      .data('origVal', initialReleaseDate ?? '')
      .data('everEdited', false);

    // Description
    const initialDescription = (ovr?.description != null) ? trimOrNull(ovr.description) : tdDescription;
    $('#ovr-description')
      .val(initialDescription ?? '')
      .data('origVal', initialDescription ?? '')
      .data('everEdited', false);

    // Version (numeric)
    const initialVersion = (ovr?.version != null) ? ovr.version : tdVersionNum;
    $('#ovr-version')
      .val((initialVersion ?? '') === '' ? '' : String(initialVersion))
      .data('origVal', (initialVersion ?? '') === '' ? '' : String(initialVersion))
      .data('everEdited', false);

    // TID display/edit
    const displayTid = pickTidForDisplay(game, ovr);
    $('#ovTitleIdDisplay').text(displayTid || '(none)');
    $('#ovCorrectedTitleId')
      .val(displayTid)
      .data('origTid', displayTid || '')
      .data('everEdited', false);

    $('#ovTitleIdEditRow').addClass('d-none');
    $('#ovTitleIdEditBtn').removeClass('d-none');

    $('#btn-reset-override').toggle(!!ovr?.id);

    // clear file inputs
    $('#ovr-banner-file').val('');
    $('#ovr-icon-file').val('');
    $('#ovr-banner-file').data('pending', null);
    $('#ovr-icon-file').data('pending', null);
    $('#ovr-banner-remove').hide();
    $('#ovr-icon-remove').hide();

    // Determine sources
    const ovrBanner = ovr?.banner_path || ovr?.bannerUrl || null;
    const ovrIcon   = ovr?.icon_path   || ovr?.iconUrl   || null;

    const gameBanner = game.banner_path || game.bannerUrl || game.banner || null;
    const gameIcon   = game.iconUrl || null;

    let currentBanner = ovrBanner ? addBuster(ovrBanner, getBuster(game.app_id)) : (gameBanner || null);
    let currentIcon   = ovrIcon   ? addBuster(ovrIcon,   getBuster(game.app_id)) : (gameIcon   || null);

    if (!currentBanner && currentIcon) {
      const img = new Image();
      img.crossOrigin = 'anonymous'; // allow CORS-safe draw if server sends ACAO
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = 400; canvas.height = 225;
        const ctx = canvas.getContext('2d');
        const scale = Math.max(400 / img.width, 225 / img.height);
        const w = Math.round(img.width * scale);
        const h = Math.round(img.height * scale);
        const dx = Math.round((400 - w) / 2);
        const dy = Math.round((225 - h) / 2);
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.clearRect(0, 0, 400, 225);
        ctx.drawImage(img, dx, dy, w, h);
        try {
          const dataURL = canvas.toDataURL('image/png');
          $('#ovr-banner-preview-img').attr('src', dataURL);
        } catch (_) {
          // Canvas tainted (no CORS) — fall back to the original image URL or default
          $('#ovr-banner-preview-img').attr('src', currentIcon || DEFAULT_BANNER());
        }
        $('#ovr-banner-preview-img').data('ovr', !!ovrBanner);
        $('#ovr-banner-remove').toggle(!!ovrBanner);
      };
      img.onerror = () => {
        $('#ovr-banner-preview-img').attr('src', DEFAULT_BANNER());
        $('#ovr-banner-preview-img').data('ovr', !!ovrBanner);
        $('#ovr-banner-remove').toggle(!!ovrBanner);
      };
      img.src = currentIcon;
    } else {
      $('#ovr-banner-preview-img').attr('src', currentBanner || DEFAULT_BANNER());
      $('#ovr-banner-preview-img').data('ovr', !!ovrBanner);
      $('#ovr-banner-remove').toggle(!!ovrBanner);
    }

    if (!currentIcon && currentBanner) {
      const img = new Image();
      img.crossOrigin = 'anonymous';
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = 400; canvas.height = 400;
        const ctx = canvas.getContext('2d');
        const scale = Math.max(400 / img.width, 400 / img.height);
        const w = Math.round(img.width * scale);
        const h = Math.round(img.height * scale);
        const dx = Math.round((400 - w) / 2);
        const dy = Math.round((400 - h) / 2);
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.clearRect(0, 0, 400, 400);
        ctx.drawImage(img, dx, dy, w, h);

        try {
          const dataURL = canvas.toDataURL('image/png');
          $('#ovr-icon-preview-img').attr('src', dataURL);
        } catch (_) {
          $('#ovr-icon-preview-img').attr('src', currentBanner || DEFAULT_ICON());
        }
        $('#ovr-icon-preview-img').data('ovr', !!ovrIcon);
        $('#ovr-icon-remove').toggle(!!ovrIcon);
      };
      img.onerror = () => {
        $('#ovr-icon-preview-img').attr('src', DEFAULT_ICON());
        $('#ovr-icon-preview-img').data('ovr', !!ovrIcon);
        $('#ovr-icon-remove').toggle(!!ovrIcon);
      };
      img.src = currentBanner;
    } else {
      $('#ovr-icon-preview-img').attr('src', currentIcon || DEFAULT_ICON());
      $('#ovr-icon-preview-img').data('ovr', !!ovrIcon);
      $('#ovr-icon-remove').toggle(!!ovrIcon);
    }

    const modalInstance = overrideModal();
    if (modalInstance) {
      modalInstance.show();
    }
  }

  const finishOverrideMutationAndRefresh = (modifiedKey) => {
    if (env.getGames) applyOverrideToGamesByKey(modifiedKey, env.getGames());
    if (env.applyFilters) env.applyFilters();
    const modalInstance = overrideModal();
    if (modalInstance) {
      modalInstance.hide();
    }
  }

  const saveOverride = async () => {
    const id = $('#ovr-id').val().trim();
    const app_id = $('#ovr-app-id').val().trim();

    const payload = {
      enabled: true
    };

    // --- Name (send only if edited & changed; allow explicit clearing)
    const $name = $('#ovr-name');
    const origName  = (trimOrNull($name.data('origName')) || '');
    const nameEdited = $name.data('everEdited') === true;
    const nameVal    = trimOrNull($name.val());

    if (nameEdited) {
      if (nameVal && nameVal !== origName) {
        // user changed to a new non-empty value -> set it
        payload.name = nameVal;
      } else if (!nameVal && origName) {
        // user cleared it -> explicitly clear on backend
        payload.name = null;
      }
    }

    // --- Region (send only if edited & changed; allow explicit clearing)
    const $region = $('#ovr-region');
    const regionEdited = $region.data('everEdited') === true;
    const regionOrig = trimOrNull($region.data('origVal')) || '';
    const regionVal  = trimOrNull($region.val());

    if (regionEdited) {
      if ((regionVal || '') !== regionOrig) {
        payload.region = (regionVal ?? null);
      }
    }

    // --- Release date (yyyy-MM-dd) same rules
    const $rd = $('#ov-release-date');
    const rdEdited = $rd.data('everEdited') === true;
    const rdOrig = trimOrNull($rd.data('origVal')) || '';
    const rdVal  = trimOrNull($rd.val()); // or null

    if (rdEdited) {
      if ((rdVal || '') !== rdOrig) {
        payload.release_date = (rdVal ?? null);
      }
    }

    // --- Description (send only if edited & changed; allow clearing)
    const $desc = $('#ovr-description');
    const descEdited = $desc.data('everEdited') === true;
    const descOrig = trimOrNull($desc.data('origVal')) || '';
    const descVal  = trimOrNull($desc.val());

    if (descEdited) {
      if ((descVal || '') !== descOrig) {
        payload.description = (descVal ?? null);
      }
    }

    // --- Version (number) — send only if edited & changed; allow clearing
    const $ver = $('#ovr-version');
    const verEdited = $ver.data('everEdited') === true;
    const verOrigStr = ($ver.data('origVal') ?? '').toString();
    const verOrigNum = numOrNull(verOrigStr);
    const verValNum  = numOrNull($ver.val());

    if (verEdited) {
      // Note: treat NaN/null as "cleared"
      const changed =
        (verValNum === null && verOrigNum !== null) ||
        (verValNum !== null && verOrigNum === null) ||
        (verValNum !== null && verOrigNum !== null && verValNum !== verOrigNum);
      if (changed) {
        payload.version = (verValNum === null ? null : verValNum);
      }
    }

    // --- Title ID override logic (include only if user edited AND changed) ---
    const $tid = $('#ovCorrectedTitleId');
    const origTid = ($tid.data('origTid') || '').toUpperCase();
    const everEdited = $tid.data('everEdited') === true;

    let correctedTitleId = trimOrNull($tid.val());
    if (correctedTitleId) {
      correctedTitleId = correctedTitleId.toUpperCase();
      if (correctedTitleId.startsWith('0X')) correctedTitleId = correctedTitleId.slice(2);
    }

    if (everEdited) {
      // If edited, only send when valid AND different from the original shown
      if (correctedTitleId) {
        if (!/^[0-9A-F]{16}$/.test(correctedTitleId)) {
          // invalid -> abort
          alert('Corrected Title ID must be exactly 16 hex characters (optionally prefixed by 0x).');
          return;
        }
        if (correctedTitleId !== origTid) {
          // changed -> include
          payload.corrected_title_id = correctedTitleId;
        }
      }
    }

    if (!id) payload.app_id = app_id;

    // Pending artwork (set by change/drop handlers)
    const bannerFileToUpload = $('#ovr-banner-file').data('pending') || null;
    const bannerRemoveRequested = $('#ovr-banner-remove').data('remove') === true;
    const iconFileToUpload = $('#ovr-icon-file').data('pending') || null;
    const iconRemoveRequested = $('#ovr-icon-remove').data('remove') === true;

    const needMultipart = !!(bannerFileToUpload || bannerRemoveRequested || iconFileToUpload || iconRemoveRequested);

    let url, method, options;
    if (id) { url = `/api/overrides/${id}`; method = 'PUT'; }
    else    { url = '/api/overrides';      method = 'POST'; }

    if (needMultipart) {
      const fd = new FormData();
      Object.entries(payload).forEach(([k, v]) => { if (v !== undefined && v !== null) fd.append(k, String(v)); });
      if (bannerFileToUpload) fd.append('banner_file', bannerFileToUpload);
      if (bannerRemoveRequested) fd.append('banner_remove', 'true');
      if (iconFileToUpload)   fd.append('icon_file', iconFileToUpload);
      if (iconRemoveRequested) fd.append('icon_remove', 'true');
      options = { method, body: fd };
    } else {
      options = { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) };
    }

    let res;
    const isFD = (options.body instanceof FormData);
    try {
      res = await $.ajax({
        url,
        type: options.method,
        data: isFD ? options.body : options.body,
        processData: false,
        contentType: isFD ? false : 'application/json',
        dataType: 'json'
      });
    } catch {
      alert('Failed to save override');
      return;
    }

    // clear pending flags
    $('#ovr-banner-file').val('').data('pending', null);
    $('#ovr-icon-file').val('').data('pending', null);
    $('#ovr-banner-remove').data('remove', false);
    $('#ovr-icon-remove').data('remove', false);

    if (res && res.app_id) {
      const key = appKey(res);
      if (key) {
        overridesByKey.set(key, res);
        if (needMultipart) bumpBuster(key); // refresh override artwork if changed

        // If a corrected ID is present, hint the current in-memory game for a badge
        if (res.corrected_title_id && env.getGames) {
          const games = env.getGames();
          const g = games.find(x => x.app_id === key);
          if (g) g.recognized_via_correction = true;
        }
        finishOverrideMutationAndRefresh(key);
        return;
      }
    }

    await fetchOverrides();
    if (needMultipart) bumpBuster(app_id);
    finishOverrideMutationAndRefresh(app_id);
  }

  const resetOverride = async () => {
    const id = $('#ovr-id').val().trim();
    if (!id) return;
    if (!confirm('Remove override and revert to default metadata?')) return;

    try {
      await $.ajax({
        url: `/api/overrides/${id}`,
        method: 'DELETE'
      });
    } catch {
      alert('Failed to delete override');
      return;
    }

    const app_id = $('#ovr-app-id').val().trim();

    if (app_id && overridesByKey.has(app_id)) {
      overridesByKey.delete(app_id);
    } else {
      await fetchOverrides();
    }

    bumpBuster(app_id);
    finishOverrideMutationAndRefresh(app_id);
  }

  // ----------------- DOM bindings for modal & DnD -----------------
  const initDomBindings = () => {
    // Banner change
    $('#ovr-banner-file').off('change').on('change', function () {
      const f = this.files && this.files[0];
      $(this).data('pending', f || null);
      $('#ovr-banner-remove').data('remove', false);

      if (!f) {
        const current = $('#ovr-banner-preview-img').data('ovr') ? $('#ovr-banner-preview-img').attr('src') : null;
        $('#ovr-banner-preview-img').attr('src', current || DEFAULT_BANNER());
        $('#ovr-banner-remove').toggle(!!$('#ovr-banner-preview-img').data('ovr'));
        return;
      }

      cropBannerFileToDataURL(f, (dataURL) => {
        if (!dataURL) {
          const fr = new FileReader();
          fr.onload = e => {
            $('#ovr-banner-preview-img').attr('src', e.target.result);
            $('#ovr-banner-remove').show();
            $('#ovr-banner-preview-img').removeData('ovr');
          };
          fr.readAsDataURL(f);
        } else {
          $('#ovr-banner-preview-img').attr('src', dataURL);
          $('#ovr-banner-remove').show();
          $('#ovr-banner-preview-img').removeData('ovr');
        }

        if (!$('#ovr-icon-file').data('pending') && !$('#ovr-icon-preview-img').data('ovr')) {
          cropIconFileToDataURL(f, (iconURL) => {
            $('#ovr-icon-preview-img').attr('src', iconURL || DEFAULT_ICON());
            if (iconURL) { $('#ovr-icon-remove').show(); $('#ovr-icon-preview-img').removeData('ovr'); }
          });
        }
      });
    });

    // Icon change
    $('#ovr-icon-file').off('change').on('change', function () {
      const f = this.files && this.files[0];
      $(this).data('pending', f || null);
      $('#ovr-icon-remove').data('remove', false);

      if (!f) {
        const current = $('#ovr-icon-preview-img').data('ovr') ? $('#ovr-icon-preview-img').attr('src') : null;
        $('#ovr-icon-preview-img').attr('src', current || DEFAULT_ICON());
        $('#ovr-icon-remove').toggle(!!$('#ovr-icon-preview-img').data('ovr'));
        return;
      }

      cropIconFileToDataURL(f, (dataURL) => {
        if (!dataURL) {
          const fr = new FileReader();
          fr.onload = e => {
            $('#ovr-icon-preview-img').attr('src', e.target.result);
            $('#ovr-icon-remove').show();
            $('#ovr-icon-preview-img').removeData('ovr');
          };
          fr.readAsDataURL(f);
        } else {
          $('#ovr-icon-preview-img').attr('src', dataURL);
          $('#ovr-icon-remove').show();
          $('#ovr-icon-preview-img').removeData('ovr');
        }

        if (!$('#ovr-banner-file').data('pending') && !$('#ovr-banner-preview-img').data('ovr')) {
          cropBannerFileToDataURL(f, (bannerURL) => {
            $('#ovr-banner-preview-img').attr('src', bannerURL || DEFAULT_BANNER());
            if (bannerURL) { $('#ovr-banner-remove').show(); $('#ovr-banner-preview-img').removeData('ovr'); }
          });
        }
      });
    });

    // Remove buttons
    $('#ovr-banner-remove').off('click').on('click', function () {
      $('#ovr-banner-file').data('pending', null).val('');
      $(this).data('remove', true);
      $('#ovr-banner-preview-img').removeData('ovr').attr('src', DEFAULT_BANNER());
      $(this).hide();
    });

    $('#ovr-icon-remove').off('click').on('click', function () {
      $('#ovr-icon-file').data('pending', null).val('');
      $(this).data('remove', true);
      $('#ovr-icon-preview-img').removeData('ovr').attr('src', DEFAULT_ICON());
      $(this).hide();
    });

    // Pencil/edit buttons
    $('#ovr-banner-edit').off('click').on('click', () => $('#ovr-banner-file').trigger('click'));
    $('#ovr-icon-edit').off('click').on('click',   () => $('#ovr-icon-file').trigger('click'));

    // Drag & drop zones
    const wireDropZone = ($wrap, kind) => {
      const over = () => $wrap.addClass('dragover');
      const out  = () => $wrap.removeClass('dragover');

      $wrap.on('dragenter dragover', (e) => { e.preventDefault(); e.stopPropagation(); over(); });
      $wrap.on('dragleave dragend drop', (e) => { e.preventDefault(); e.stopPropagation(); out(); });

      $wrap.on('drop', (e) => {
        const dt = e.originalEvent.dataTransfer;
        if (!dt || !dt.files || !dt.files.length) return;

        const file = dt.files[0];
        if (!file || !file.type || !file.type.startsWith('image/')) return;

        if (kind === 'banner') {
          try { $('#ovr-banner-file')[0].files = dt.files; $('#ovr-banner-file').trigger('change'); }
          catch {
            $('#ovr-banner-file').data('pending', file);
            $('#ovr-banner-remove').data('remove', false);
            cropBannerFileToDataURL(file, (dataURL) => {
              if (dataURL) {
                $('#ovr-banner-preview-img').attr('src', dataURL).removeData('ovr');
                $('#ovr-banner-remove').show();
              }
              if (!$('#ovr-icon-preview-img').data('ovr')) {
                cropIconFileToDataURL(file, (iconURL) => {
                  $('#ovr-icon-preview-img').attr('src', iconURL || DEFAULT_ICON());
                  if (iconURL) { $('#ovr-icon-remove').show(); $('#ovr-icon-preview-img').removeData('ovr'); }
                });
              }
            });
          }
        } else {
          try { $('#ovr-icon-file')[0].files = dt.files; $('#ovr-icon-file').trigger('change'); }
          catch {
            $('#ovr-icon-file').data('pending', file);
            $('#ovr-icon-remove').data('remove', false);
            cropIconFileToDataURL(file, (dataURL) => {
              if (dataURL) {
                $('#ovr-icon-preview-img').attr('src', dataURL).removeData('ovr');
                $('#ovr-icon-remove').show();
              }
              if (!$('#ovr-banner-preview-img').data('ovr')) {
                cropBannerFileToDataURL(file, (bannerURL) => {
                  $('#ovr-banner-preview-img').attr('src', bannerURL || DEFAULT_BANNER());
                  if (bannerURL) { $('#ovr-banner-remove').show(); $('#ovr-banner-preview-img').removeData('ovr'); }
                });
              }
            });
          }
        }
      });
    }

    wireDropZone($('#ovr-banner-preview'), 'banner');
    wireDropZone($('#ovr-icon-preview'),   'icon');

    // Save/Reset buttons
    $('#btn-save-override').off('click').on('click', saveOverride);
    $('#btn-reset-override').off('click').on('click', resetOverride);

    // Date picker (if supported)
    $('#ov-release-date').off('click').on('click', function() { this.showPicker?.(); });

    // Mark "everEdited" for region/description/version/release_date
    $('#ovr-region').off('input').on('input', function(){ $(this).data('everEdited', true); });
    $('#ovr-description').off('input').on('input', function(){ $(this).data('everEdited', true); });
    $('#ovr-version').off('input').on('input', function(){ $(this).data('everEdited', true); });
    $('#ov-release-date').off('change input').on('change input', function(){ $(this).data('everEdited', true); });

    // Title ID "click to edit"
    $('#ovTitleIdEditBtn').off('click').on('click', function () {
      $('#ovTitleIdEditRow').removeClass('d-none');
      $('#ovTitleIdEditBtn').addClass('d-none');
      // Mark that the user intentionally entered edit mode
      $('#ovCorrectedTitleId').data('everEdited', true);
      // Focus input and place caret at end
      const $inp = $('#ovCorrectedTitleId');
      const v = $inp.val() || '';
      $inp.focus().val('').val(v);
    });

    // Also allow clicking the displayed Title ID to enter edit mode
    $('#ovTitleIdDisplay').off('click').on('click', function () {
      $('#ovTitleIdEditBtn').trigger('click');
    });

    // Cancel returns to collapsed view (discard any unsaved edits)
    $('#ovTitleIdCancelBtn').off('click').on('click', function () {
      const orig = $('#ovCorrectedTitleId').data('origTid') || '';
      $('#ovCorrectedTitleId').val(orig);
      // User backed out; treat as never-edited for saving
      $('#ovCorrectedTitleId').data('everEdited', false);
      $('#ovTitleIdEditRow').addClass('d-none');
      $('#ovTitleIdEditBtn').removeClass('d-none');
    });

    // Mark that the Name field was intentionally edited
    $('#ovr-name').off('input').on('input', function () {
      $(this).data('everEdited', true);
    });
  }

  // ----------------- Public API -----------------
  const overridesApi = {
    // wiring
    bindEnvironment(opts = {}) {
      env.getGames = opts.getGames || null;
      env.applyFilters = opts.applyFilters || null;
    },
    initDomBindings,

    // fetching/overlay
    fetchOverrides,
    reapplyAllOverridesToGames,
    hasActiveOverride,
    bannerUrlFor,
    iconUrlFor,
    openOverrideEditor,

    // flags helpers to compute once per game
    computeRecognitionFlags,
    isUnrecognizedGame,
    pickTidForDisplay,
    getOverrideForGame,
    displayTitleFor,

    // redirects
    getRedirectForApp,
    applyRedirectToGame,
    applyRedirectsToGames,
  };
  namespace.Overrides = overridesApi;
})(window, window.jQuery);
