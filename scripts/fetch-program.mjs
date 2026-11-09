// Τρέχει στο GitHub Actions: διαβάζει το Athletiko και γράφει program.json
import fs from "node:fs";

const res = await fetch("https://www.athletiko.gr/tvprogram", {
  headers: { "User-Agent": "Mozilla/5.0 (compatible; TV-live-sport/2.0)", "Accept-Language": "el" },
});
if (!res.ok) { console.error("HTTP " + res.status); process.exit(1); }
const html = await res.text();

const text = html
  .replace(/<(script|style|noscript)[\s\S]*?<\/\1>/gi, " ")
  .replace(/<img[^>]*alt="([^"]*)"[^>]*>/gi, "\n$1\n") // τα κανάλια είναι λογότυπα με alt
  .replace(/<[^>]+>/g, "\n")
  .replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#0?39;/g, "'").replace(/&nbsp;/g, " ");
const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);

const offset = (d) =>
  new Intl.DateTimeFormat("en", { timeZone: "Europe/Athens", timeZoneName: "longOffset" })
    .formatToParts(d).find((p) => p.type === "timeZoneName").value.replace("GMT", "") || "+00:00";
const today = new Intl.DateTimeFormat("sv", { timeZone: "Europe/Athens" }).format(new Date());

const timeRe = /^([01]\d|2[0-3]):[0-5]\d$/;
const events = [], seen = new Set();
let day = 0, prev = null;
for (let i = 0; i < lines.length; i++) {
  if (!timeRe.test(lines[i])) continue;
  const [c, t, h] = [lines[i - 1] || "", lines[i + 1] || "", lines[i + 2] || "—"];
  if (t.length < 2 || timeRe.test(t)) continue;
  const [hh, mm] = lines[i].split(":").map(Number);
  const cur = hh * 60 + mm;
  if (prev !== null && cur < prev - 180) day++; // η λίστα είναι χρονολογική: πτώση ώρας = νέα μέρα
  prev = cur;
  const base = new Date(today + "T12:00:00Z");
  base.setUTCDate(base.getUTCDate() + day);
  const ymd = base.toISOString().slice(0, 10);
  const s = new Date(`${ymd}T${lines[i]}:00${offset(base)}`).getTime();
  const key = s + "|" + t;
  if (seen.has(key)) continue;
  seen.add(key);
  events.push({ s, c, t, h: h.length > 40 ? h.slice(0, 40) : h });
}

if (events.length < 10) { console.error("Βρέθηκαν μόνο " + events.length + " μεταδόσεις. Δεν γράφω αρχείο."); process.exit(1); }
events.sort((a, b) => a.s - b.s);
fs.writeFileSync("program.json", JSON.stringify({ updated: Date.now(), events }));
console.log("OK: " + events.length + " μεταδόσεις");
