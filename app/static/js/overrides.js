// Global helper for Overrides functionality.
// Attaches as window.Overrides (no module build needed).

(() => {
  const DEFAULT_BANNER = window.DEFAULT_BANNER || 'https://placehold.co/400x225/png?text=Image+Unavailable';
  const DEFAULT_ICON   = window.DEFAULT_ICON   || 'https://placehold.co/400x400/png?text=Image+Unavailable';

  // Forces browsers to refetch updated artwork after saves/resets
  const ARTWORK_BUSTERS = new Map(); // app_id -> integer
  const getBuster = (appId) => ARTWORK_BUSTERS.get(appId) || 0;
  const bumpBuster = (appId) => ARTWORK_BUSTERS.set(appId, getBuster(appId) + 1);

  // --- Overrides state ---
  // key: app_id  -> override object
  const overridesByKey = new Map();

  // external environment (supplied by index.html)
  const env = {
    getGames: null,      // () => games array
    applyFilters: null   // () => void
  };

  const overrideEl = document.getElementById('overrideEditorModal');
  const overrideModal = () => bootstrap.Modal.getOrCreateInstance(overrideEl);

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

  // Recognition flags (stable against overrides)
  const computeRecognitionFlags = (game) => {
    const tidNameRaw = (game?._orig?.title_id_name ?? game.title_id_name ?? '').trim();
    const hasTitleDb =
      !!tidNameRaw &&
      tidNameRaw.toLowerCase() !== 'unrecognized' &&
      tidNameRaw.toLowerCase() !== 'unidentified';
    return { isUnrecognized: !hasTitleDb, hasTitleDb };
  }

  const isUnrecognizedGame = (game) => {
    if (!game) return false;
    if (typeof game.isUnrecognized === 'boolean') return game.isUnrecognized;
    const flags = computeRecognitionFlags(game);
    game.isUnrecognized = flags.isUnrecognized;
    game.hasTitleDb = flags.hasTitleDb;
    return game.isUnrecognized;
  }

  // ----------------- Overlay helpers -----------------
  // Apply (or remove) a single override onto matching games in memory.
  const applyOverrideToGamesByKey = (key, games) => {
    if (!key || !Array.isArray(games)) return;
    const ovr = overridesByKey.get(key) || null;
    const affecteds = games.filter(g => appKey(g) === key);

    affecteds.forEach(g => {
      if (!g._orig) {
        g._orig = { name: g.name, title_id_name: g.title_id_name, release_date: g.release_date ?? null };
      }

      if (ovr && ovr.enabled !== false) {
        if (ovr.name && typeof ovr.name === 'string' && ovr.name.trim().length) {
          g.title_id_name = ovr.name;
          g.name = ovr.name;
        }
        // apply release_date if present (allow clearing with null)
        if ('release_date' in ovr) {
          g.release_date = ovr.release_date ?? null; // expected yyyy-MM-dd string (server sends ISO)
        }
      } else {
        // restore originals
        if (g._orig) {
          g.title_id_name = g._orig.title_id_name;
          g.name = g._orig.name;
          g.release_date = g._orig.release_date ?? null;
        }
      }
    });
  }

  const reapplyAllOverridesToGames = (games) => {
    if (!Array.isArray(games)) return;
    const keys = new Set();
    games.forEach(g => keys.add(appKey(g)));
    keys.forEach(k => applyOverrideToGamesByKey(k, games));
  }

  // ----------------- Fetching -----------------
  const fetchOverrides = async () => {
    try {
      const list = await $.ajax({
        url: '/api/overrides',
        method: 'GET',
        dataType: 'json',
        cache: false
      });

      overridesByKey.clear();
      (Array.isArray(list.items) ? list.items : []).forEach(o => {
        const k = appKey(o);
        if (k) overridesByKey.set(k, o);
      });

      if (env.getGames) reapplyAllOverridesToGames(env.getGames());
    } catch (e) {
      overridesByKey.clear();
    }
  };

  // ----------------- Derived artwork URLs -----------------
  const bannerUrlFor = (game) => {
    const ovr = getOverrideForGame(game);
    const ovrUrl = _trimmedOrNull(ovr?.banner_path) || _trimmedOrNull(ovr?.bannerUrl);
    if (ovrUrl) return addBuster(ovrUrl, getBuster(game.app_id));

    return (
      _trimmedOrNull(game.banner_path) || _trimmedOrNull(game.bannerUrl) || _trimmedOrNull(game.banner) ||
      _trimmedOrNull(game.iconUrl) || DEFAULT_BANNER
    );
  }

  const iconUrlFor = (game) =>{
    const ovr = getOverrideForGame(game);
    const ovrUrl = _trimmedOrNull(ovr?.icon_path) || _trimmedOrNull(ovr?.iconUrl);
    if (ovrUrl) return addBuster(ovrUrl, getBuster(game.app_id));

    return (
      _trimmedOrNull(game.iconUrl) || _trimmedOrNull(game.icon) ||
      _trimmedOrNull(game.banner_path) || _trimmedOrNull(game.bannerUrl) || _trimmedOrNull(game.banner) ||
      DEFAULT_ICON
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

    $('#ovr-name').val(ovr?.name ?? (game.title_id_name || game.name || ''));
    $('#ovr-region').val(ovr?.region ?? '');
    $('#ov-release-date').val(ovr?.release_date ?? (game.release_date || ''));
    $('#ovr-description').val(ovr?.description ?? '');
    $('#ovr-version').val(ovr?.version ?? '');

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
          $('#ovr-banner-preview-img').attr('src', currentIcon || DEFAULT_BANNER);
        }
        $('#ovr-banner-preview-img').data('ovr', !!ovrBanner);
        $('#ovr-banner-remove').toggle(!!ovrBanner);
      };
      img.onerror = () => {
        $('#ovr-banner-preview-img').attr('src', DEFAULT_BANNER);
        $('#ovr-banner-preview-img').data('ovr', !!ovrBanner);
        $('#ovr-banner-remove').toggle(!!ovrBanner);
      };
      img.src = currentIcon;
    } else {
      $('#ovr-banner-preview-img').attr('src', currentBanner || DEFAULT_BANNER);
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
          $('#ovr-icon-preview-img').attr('src', currentBanner || DEFAULT_ICON);
        }
        $('#ovr-icon-preview-img').data('ovr', !!ovrIcon);
        $('#ovr-icon-remove').toggle(!!ovrIcon);
      };
      img.onerror = () => {
        $('#ovr-icon-preview-img').attr('src', DEFAULT_ICON);
        $('#ovr-icon-preview-img').data('ovr', !!ovrIcon);
        $('#ovr-icon-remove').toggle(!!ovrIcon);
      };
      img.src = currentBanner;
    } else {
      $('#ovr-icon-preview-img').attr('src', currentIcon || DEFAULT_ICON);
      $('#ovr-icon-preview-img').data('ovr', !!ovrIcon);
      $('#ovr-icon-remove').toggle(!!ovrIcon);
    }

    overrideModal().show();
  }

  const finishOverrideMutationAndRefresh = (modifiedKey) => {
    if (env.getGames) applyOverrideToGamesByKey(modifiedKey, env.getGames());
    if (env.applyFilters) env.applyFilters();
    overrideModal().hide();
  }

  const saveOverride = async () => {
    const id = $('#ovr-id').val().trim();
    const app_id = $('#ovr-app-id').val().trim();

    const payload = {
      name: trimOrNull($('#ovr-name').val()),
      region: trimOrNull($('#ovr-region').val()),
      description: trimOrNull($('#ovr-description').val()),
      version: numOrNull($('#ovr-version').val()),
      release_date: trimOrNull($('#ov-release-date').val()), // yyyy-MM-dd or null
      enabled: true
    };
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
        data: isFD ? options.body : options.body, // FormData or JSON string
        processData: false,                        // keep raw body for both cases
        contentType: isFD ? false : 'application/json',
        dataType: 'json'                           // parse JSON response for you
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
        $('#ovr-banner-preview-img').attr('src', current || DEFAULT_BANNER);
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
            $('#ovr-icon-preview-img').attr('src', iconURL || DEFAULT_ICON);
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
        $('#ovr-icon-preview-img').attr('src', current || DEFAULT_ICON);
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
            $('#ovr-banner-preview-img').attr('src', bannerURL || DEFAULT_BANNER);
            if (bannerURL) { $('#ovr-banner-remove').show(); $('#ovr-banner-preview-img').removeData('ovr'); }
          });
        }
      });
    });

    // Remove buttons
    $('#ovr-banner-remove').off('click').on('click', function () {
      $('#ovr-banner-file').data('pending', null).val('');
      $(this).data('remove', true);
      $('#ovr-banner-preview-img').removeData('ovr').attr('src', DEFAULT_BANNER);
      $(this).hide();
    });

    $('#ovr-icon-remove').off('click').on('click', function () {
      $('#ovr-icon-file').data('pending', null).val('');
      $(this).data('remove', true);
      $('#ovr-icon-preview-img').removeData('ovr').attr('src', DEFAULT_ICON);
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
                  $('#ovr-icon-preview-img').attr('src', iconURL || DEFAULT_ICON);
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
                  $('#ovr-banner-preview-img').attr('src', bannerURL || DEFAULT_BANNER);
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
  }

  // ----------------- Public API -----------------
  window.Overrides = {
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
    isUnrecognizedGame
  };
})();
