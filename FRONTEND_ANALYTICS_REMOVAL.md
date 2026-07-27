# Two third-party scripts removed from the HQ console

`frontend/public/index.html` loaded two external scripts on every page. Both are
gone. The first is housekeeping; the second is not.

## 1. `https://assets.emergent.sh/scripts/emergent-main.js`

Scaffolding from the template this project started as. It executed on every HQ
page load and has no role in a private retail announcement system.

## 2. PostHog, with session recording

```js
posthog.init("phc_…", {
    session_recording: {
        recordCrossOriginIframes: true,
        capturePerformance: false,
    },
});
```

**Session recording captures the operator's screen**, and this console displays
two things exactly once and never again:

- a one-time Receiver **enrolment code**, on the Device page;
- a rotated Device **credential**, immediately after a rotation.

The server keeps a verifier, not the value, *precisely* so neither can be
recovered afterwards. A recording of the screen that showed them is a copy the
credential design says cannot exist — and it would be created silently, because
nothing in the product tells an operator that recording is happening.

Beyond credentials, it sent the browsing of a private 44-Store estate, under a
hard-coded project key, to an external host nobody has approved as a data
processor.

This is the same class of problem as a credential in a URL: not an attack, just
a copy of a secret arriving somewhere nobody thought about.

### Tests that keep it out

`frontend/e2e/branding.spec.js` asserts, on both the console and the Device page
that renders credentials:

- no request reaches `posthog.com` or `emergent.sh`;
- `window.posthog` and `window.rrweb` are undefined;
- the page source contains no `phc_…` key, no `session_recording`, and no
  `recordCrossOriginIframes`.

The explanation lives in this file rather than in an HTML comment. Comments in
`index.html` are served to the browser, so the first version of that note put
the strings it was warning about back into the page and failed its own test.

### If analytics are wanted later

Three decisions first: which events are collected, which processor is approved,
and session recording **off** on any page that can display a credential — which
today means the Receiver Devices page and anything that replaces it.

## Still outstanding

`frontend/package.json` depends on
`@emergentbase/visual-edits` from `https://assets.emergent.sh/npm/…`. That is a
build-time dependency, not a script in the shipped page, so it is out of scope
for this change — but a private product taking a build dependency from a
template vendor's asset host is worth a deliberate decision rather than
inheritance.
