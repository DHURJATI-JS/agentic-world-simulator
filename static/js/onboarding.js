/* First-run onboarding chat with the AI + the reusable startup profile form. */
(function () {
  'use strict';
  const { esc } = window.U;

  function csv(list) { return (list || []).join(', '); }
  function splitCsv(v) { return String(v || '').split(',').map((s) => s.trim()).filter(Boolean); }

  function profileForm(p, { submitLabel = 'Save profile', note = '' } = {}) {
    p = p || {};
    const cb = (name, label, hint) => `<label class="check"><input type="checkbox" name="${name}" ${p[name] ? 'checked' : ''}> <span>${label}${hint ? ` <span class="muted small">${hint}</span>` : ''}</span></label>`;
    return `<form class="profile-form">
      ${note ? `<p class="small muted" style="margin-top:0">${note}</p>` : ''}
      <div class="grid-2">
        <label class="field"><span>Startup name *</span><input type="text" name="name" value="${esc(p.name)}" required maxlength="200"></label>
        <label class="field"><span>Sector</span><input type="text" name="sector" value="${esc(p.sector)}" maxlength="200"></label>
      </div>
      <label class="field"><span>What does it do?</span><textarea name="description" rows="2" maxlength="2000">${esc(p.description)}</textarea></label>
      <div class="grid-2">
        <label class="field"><span>Offerings (comma separated)</span><input type="text" name="offerings" value="${esc(csv(p.offerings))}"></label>
        <label class="field"><span>Business model</span><input type="text" name="business_model" value="${esc(p.business_model)}" placeholder="B2B SaaS, D2C, marketplace…"></label>
      </div>
      <h4 style="margin:6px 0">Capabilities (filters every recommendation)</h4>
      ${cb('sells_physical_products', 'Sells physical products')}
      ${cb('can_ship_domestic', 'Can ship products domestically')}
      ${cb('can_ship_international', 'Can ship products internationally')}
      <label class="field" style="margin-left:26px"><span>Ships to (country names/codes or "worldwide")</span><input type="text" name="ships_to" value="${esc(csv(p.ships_to))}"></label>
      ${cb('offers_services', 'Offers services')}
      ${cb('digital_only', 'Digital-only (software / online)')}
      <div class="grid-2" style="margin-top:8px">
        <label class="field"><span>HQ city</span><input type="text" name="hq_city" value="${esc(p.hq_city)}"></label>
        <label class="field"><span>HQ country *</span><input type="text" name="hq_country" value="${esc(p.hq_country)}" required placeholder="India / IN"></label>
        <label class="field"><span>Target markets</span><input type="text" name="target_markets" value="${esc(csv(p.target_markets))}" placeholder="IN, US, JP"></label>
        <label class="field"><span>Existing insurance</span><input type="text" name="insured_for" value="${esc(csv(p.insured_for))}"></label>
        <label class="field"><span>Stage</span><input type="text" name="stage" value="${esc(p.stage)}" placeholder="pre-seed, seed, series A…"></label>
        <label class="field"><span>Team size</span><input type="text" name="team_size" value="${esc(p.team_size)}"></label>
      </div>
      <p class="small form-error" style="color:var(--down-text);min-height:16px"></p>
      <button class="btn primary" type="submit">${esc(submitLabel)}</button>
    </form>`;
  }

  function readForm(form) {
    const f = form.elements;
    return {
      name: f.name.value.trim(), description: f.description.value.trim(), sector: f.sector.value.trim(),
      business_model: f.business_model.value.trim(), offerings: splitCsv(f.offerings.value),
      sells_physical_products: f.sells_physical_products.checked, can_ship_domestic: f.can_ship_domestic.checked,
      can_ship_international: f.can_ship_international.checked, ships_to: splitCsv(f.ships_to.value),
      offers_services: f.offers_services.checked, digital_only: f.digital_only.checked,
      hq_city: f.hq_city.value.trim(), hq_country: f.hq_country.value.trim(),
      target_markets: splitCsv(f.target_markets.value), insured_for: splitCsv(f.insured_for.value),
      stage: f.stage.value.trim(), team_size: f.team_size.value.trim(),
    };
  }

  function bindForm(container, onSubmit) {
    const form = container.querySelector('.profile-form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const err = form.querySelector('.form-error');
      const btn = form.querySelector('button[type=submit]');
      err.textContent = '';
      btn.disabled = true;
      try { await onSubmit(readForm(form)); } catch (ex) { err.textContent = ex.message || String(ex); } finally { btn.disabled = false; }
    });
  }

  const Onboarding = {
    overlay: null,
    open(messages) {
      this.overlay = document.getElementById('onboarding');
      this.chat = document.getElementById('onb-chat');
      this.overlay.hidden = false;
      document.getElementById('onb-chat-wrap').hidden = false;
      document.getElementById('onb-confirm').hidden = true;
      this.chat.innerHTML = '';
      (messages || []).forEach((m) => this.add(m.role, m.content));
      if (!this.bound) {
        this.bound = true;
        document.getElementById('onb-form').addEventListener('submit', (e) => { e.preventDefault(); this.send(); });
        document.getElementById('onb-form-link').addEventListener('click', (e) => { e.preventDefault(); this.confirm({}, true); });
      }
      setTimeout(() => document.getElementById('onb-input').focus(), 50);
    },
    add(role, text) {
      const div = document.createElement('div');
      div.className = `msg ${role === 'user' ? 'user' : 'assistant'}`;
      div.textContent = text;
      this.chat.appendChild(div);
      this.chat.scrollTop = this.chat.scrollHeight;
      return div;
    },
    async send() {
      const input = document.getElementById('onb-input');
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      this.add('user', text);
      const typing = this.add('assistant', 'Thinking…');
      typing.classList.add('typing');
      const btn = document.getElementById('onb-send');
      btn.disabled = true;
      try {
        const res = await window.API.post('/api/onboarding/message', { message: text });
        typing.remove();
        this.add('assistant', res.reply);
        if (res.done) this.confirm(res.profile, false);
      } catch (ex) {
        typing.remove();
        this.add('assistant', `⚠ ${ex.message}`);
      } finally { btn.disabled = false; input.focus(); }
    },
    confirm(profile, manual) {
      const box = document.getElementById('onb-confirm');
      document.getElementById('onb-chat-wrap').hidden = true;
      box.hidden = false;
      box.innerHTML = profileForm(profile, {
        submitLabel: 'Confirm & start',
        note: manual ? 'Describe your startup. Capabilities decide which recommendations you get.'
                     : 'Here is what the AI understood. Correct anything that is off, then confirm.',
      }) + (manual ? '' : '<p class="small"><a href="#" id="onb-back">← back to the chat</a></p>');
      bindForm(box, async (data) => {
        await window.API.post('/api/onboarding/confirm', data);
        this.overlay.hidden = true;
        if (window.App) window.App.afterProfileSaved();
      });
      const back = document.getElementById('onb-back');
      if (back) back.addEventListener('click', (e) => { e.preventDefault(); box.hidden = true; document.getElementById('onb-chat-wrap').hidden = false; });
    },
  };

  window.Onboarding = Onboarding;
  window.ProfileForm = { html: profileForm, bind: bindForm };
})();
