// Click the toolbar button on a job page: read it, hand it to the local app, open the review window.
importScripts("reader.js");

const DEFAULT_APP = "http://127.0.0.1:8000";

async function say(tabId, badge, message) {
  await chrome.action.setBadgeBackgroundColor({tabId, color: badge === "!" ? "#c0392b" : "#1a7f4b"});
  await chrome.action.setBadgeText({tabId, text: badge});
  await chrome.action.setTitle({tabId, title: message});
}

chrome.action.onClicked.addListener(async tab => {
  const {token, appUrl} = await chrome.storage.local.get(["token", "appUrl"]);
  const app = (appUrl || DEFAULT_APP).replace(/\/+$/, "");
  if (!token) { await chrome.runtime.openOptionsPage(); return; }

  let page;
  try {
    const [res] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: readPage});
    page = res && res.result;
  } catch (e) {
    return say(tab.id, "!", "Job Tracker can't read this page (browser pages and the web store are off limits).");
  }
  if (!page || !page.text) return say(tab.id, "!", "Nothing to read on this page. Select the job description and click again.");

  let reply;
  try {
    reply = await fetch(app + "/api/capture", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Capture-Token": token},
      body: JSON.stringify(page),
    });
  } catch (e) {
    return say(tab.id, "!", `Can't reach the app at ${app}. Is it running?`);
  }
  if (!reply.ok) {
    let detail = "";
    try { detail = (await reply.json()).detail; } catch (e) { /* not JSON */ }
    return say(tab.id, "!", detail || `The app answered ${reply.status}.`);
  }
  const {path} = await reply.json();
  await say(tab.id, "", "Add this job to Job Tracker");
  await chrome.windows.create({url: app + path, type: "popup", width: 640, height: 820});
});
