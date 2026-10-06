const $ = id => document.getElementById(id);

chrome.storage.local.get(["token", "appUrl"]).then(v => {
  $("token").value = v.token || "";
  $("app").value = v.appUrl || "";
});

$("save").addEventListener("click", async () => {
  const token = $("token").value.trim();
  const appUrl = $("app").value.trim().replace(/\/+$/, "");
  const msg = $("msg");
  if (!token) { msg.textContent = "Paste the capture token first."; return; }
  if (appUrl && !/^http:\/\/(127\.0\.0\.1|localhost):8000$/.test(appUrl)) {
    msg.textContent = "The app address must be http://127.0.0.1:8000 or http://localhost:8000.";
    return;
  }
  await chrome.storage.local.set({token, appUrl});
  msg.textContent = "Saved. Open a job page and click the toolbar button.";
});
