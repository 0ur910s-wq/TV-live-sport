import express from 'express';
import { exec, spawn } from 'child_process';
import path from 'path';
import fs from 'fs';
import puppeteer from 'puppeteer';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = 3000;
const HISTORY_FILE = path.join(__dirname, 'history.json');
const COOKIES_PATH = path.join(__dirname, 'cookies.txt');

// === Auto-scan ρυθμίσεις ===
const AUTO_SCAN_INTERVAL_MS = 10 * 60 * 1000; // κάθε 10 λεπτά
let autoScanEnabled = true;
let autoScanTimer = null;
let isScanning = false;
let lastScanTime = null;
let lastScanResult = { success: false, addedCount: 0, total: 0 };
let retryMissingStreamsRunning = false;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

function parseNetscapeCookies(filePath) {
    if (!fs.existsSync(filePath)) return [];
    const content = fs.readFileSync(filePath, 'utf8');
    const cookies = [];
    content.split('\n').forEach(line => {
        if (!line || line.startsWith('#')) return;
        const parts = line.split('\t');
        if (parts.length >= 7) {
            cookies.push({
                name: parts[5].trim(),
                value: parts[6].trim(),
                domain: parts[0].trim(),
                path: parts[2].trim(),
                secure: parts[3].trim() === 'TRUE',
                httpOnly: false,
                expires: parts[4] && parts[4] !== '0' ? Number(parts[4]) : undefined
            });
        }
    });
    return cookies;
}

function saveUniqueSpaces(newSpaces) {
    try {
        if (!fs.existsSync(HISTORY_FILE)) {
            fs.writeFileSync(HISTORY_FILE, JSON.stringify([], null, 2));
        }
        let data = fs.readFileSync(HISTORY_FILE, 'utf8');
        let parsed = JSON.parse(data);
        let spaces = Array.isArray(parsed) ? parsed : (parsed.spaces || []);

        // Ποια ήταν live ΠΡΙΝ από αυτή τη σάρωση (για να εντοπίσουμε ποια μόλις τελείωσαν)
        const previouslyLiveIds = new Set(spaces.filter(s => s.isLive).map(s => s.spaceId));

        // Όλα παλιά: όχι NEW, όχι LIVE
        spaces.forEach(s => {
            s.isNew = false;
            s.isLive = false;
        });

        let addedCount = 0;
        const foundIds = new Set(newSpaces.map(s => s.spaceId));

        newSpaces.forEach(ns => {
            let existingIndex = spaces.findIndex(s => s.spaceId === ns.spaceId);
            if (existingIndex !== -1) {
                // Κρατάμε την αρχική ημερομηνία/ώρα, ενημερώνουμε τα υπόλοιπα
                const existing = spaces[existingIndex];
                const prevMax = existing.maxListeners || 0;
                const newCount = typeof ns.listeners === 'number' ? ns.listeners : 0;
                spaces[existingIndex] = {
                    ...existing,
                    title: ns.title || existing.title,
                    host: ns.host || existing.host,
                    url: ns.url || existing.url,
                    streamUrl: ns.streamUrl || existing.streamUrl,
                    lastScan: ns.lastScan || ns.time,
                    isLive: true,   // βρέθηκε στην τρέχουσα σάρωση = LIVE
                    isNew: true,
                    listeners: typeof ns.listeners === 'number' ? ns.listeners : existing.listeners,
                    maxListeners: Math.max(prevMax, newCount)
                };
                const updated = spaces.splice(existingIndex, 1)[0];
                spaces.unshift(updated);
                addedCount++;
            } else {
                ns.isNew = true;
                ns.isLive = true;
                ns.lastScan = ns.time;
                ns.maxListeners = typeof ns.listeners === 'number' ? ns.listeners : 0;
                spaces.unshift(ns);
                addedCount++;
            }
        });

        // Spaces που ΜΟΛΙΣ σταμάτησαν να είναι live σε αυτή τη σάρωση, και δεν έχουμε ήδη ομιλητές τους
        const justEnded = spaces.filter(s =>
            previouslyLiveIds.has(s.spaceId) &&
            !foundIds.has(s.spaceId) &&
            (!s.speakers || s.speakers.length === 0)
        );

        fs.writeFileSync(HISTORY_FILE, JSON.stringify(spaces, null, 2));
        return { spaces, addedCount, justEnded };
    } catch (err) {
        console.error('Σφάλμα αποθήκευσης ιστορικού:', err);
        return { spaces: [], addedCount: 0, justEnded: [] };
    }
}

// Κοινή λογική σάρωσης (χρησιμοποιείται και από manual και από auto)
async function doScan() {
    if (isScanning) {
        console.log('Σάρωση ήδη σε εξέλιξη, παραλείπεται...');
        return { success: false, error: 'Ήδη σαρώνεται', spaces: [], addedCount: 0 };
    }

    isScanning = true;
    let browser;

    try {
        browser = await puppeteer.launch({
            headless: 'new',
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--window-size=1280,900'
            ]
        });

        const page = await browser.newPage();
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36');
        await page.setViewport({ width: 1280, height: 900 });

        await page.evaluateOnNewDocument(() => {
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
        });

        const cookies = parseNetscapeCookies(COOKIES_PATH);
        if (cookies.length > 0) {
            await page.setCookie(...cookies);
            console.log(`Φορτώθηκαν ${cookies.length} cookies`);
        } else {
            console.log('ΠΡΟΣΟΧΗ: Δεν βρέθηκε cookies.txt');
        }

        await page.goto('https://x.com/i/spacebar/', { waitUntil: 'networkidle2', timeout: 50000 });
        await new Promise(r => setTimeout(r, 7000));
        await page.evaluate(() => window.scrollBy(0, 400));
        await new Promise(r => setTimeout(r, 2000));

        const spacesData = await page.evaluate(() => {
            const results = [];
            const seen = new Set();

            const exactNoise = new Set([
                'ακούστε ζωντανά', 'ακούστε live', 'διοργανωτής', 'listen live',
                'hosting', 'live', 'join', 'speakers', 'listeners', 'recording',
                'replay', 'ended', 'ακροατές', 'ομιλητές', 'ζωντανά', 'ηχογράφηση',
                'spaces', 'space', 'host', 'διοργανωτες'
            ]);

            const isNoiseLine = (text) => {
                const t = text.toLowerCase().trim();
                if (!t || t.length < 2) return true;
                if (/^\d+[\d,.kKmMβ\s]*$/.test(t)) return true;
                if (exactNoise.has(t)) return true;
                if (/\d+\s*(σε αυτόν|in this|listeners|ακροατ|people)/i.test(t)) return true;
                if (/^(host|διοργανωτ)/i.test(t)) return true;
                return false;
            };

            const anchors = Array.from(document.querySelectorAll('a[href*="/i/spaces/"]'));

            anchors.forEach(a => {
                const match = a.href.match(/\/i\/spaces\/([a-zA-Z0-9]+)/);
                if (!match) return;
                const spaceId = match[1];
                if (seen.has(spaceId)) return;
                seen.add(spaceId);

                let card = a.closest('[data-testid="cellInnerDiv"]')
                        || a.closest('[data-testid="cellInnerDefinition"]')
                        || a.closest('div[role="button"]')
                        || a.closest('div[role="link"]')
                        || a.closest('article')
                        || a.parentElement?.parentElement?.parentElement
                        || a.parentElement?.parentElement
                        || a.parentElement;

                let host = 'Άγνωστος';
                let title = 'Ενεργό Space';
                let listeners = null;

                // Μετατροπή συμβολοσειράς αριθμού ακροατών (π.χ. "1.2K", "3,4χιλ.", "42") σε ακέραιο
                function parseListenerNum(raw) {
                    if (!raw) return null;
                    let s = raw.trim().replace(/χιλ\.?/i, 'K').replace(/εκ\.?/i, 'M');
                    const mult = /[Mm]$/.test(s) ? 1e6 : (/[Kk]$/.test(s) ? 1e3 : 1);
                    s = s.replace(/[KkMm]$/, '');
                    if (s.includes(',') && s.includes('.')) {
                        s = s.replace(/\./g, '').replace(',', '.');
                    } else if (s.includes(',')) {
                        s = s.replace(',', '.');
                    } else if (s.includes('.') && mult === 1) {
                        s = s.replace(/\./g, '');
                    }
                    const num = parseFloat(s);
                    return isNaN(num) ? null : Math.round(num * mult);
                }

                if (card) {
                    const cardText = card.innerText || '';
                    const listenerMatch = cardText.match(/([\d.,]+\s*[KkMm]?|[\d.,]+\s*(?:χιλ|εκ)\.?)\s*(?:σε αυτόν|in this|are here|listening|ακροατ)/i);
                    if (listenerMatch) {
                        listeners = parseListenerNum(listenerMatch[1]);
                    }

                    const ariaCandidates = [
                        a.getAttribute('aria-label'),
                        card.getAttribute('aria-label'),
                        ...Array.from(card.querySelectorAll('[aria-label]')).map(el => el.getAttribute('aria-label'))
                    ].filter(Boolean);

                    for (const aria of ariaCandidates) {
                        let m = aria.match(/^(.+?)\s+by\s+(.+)$/i)
                              || aria.match(/^(.+?)\s+is hosting\s+(.+)$/i)
                              || aria.match(/^(.+?)\s+·\s+(.+)$/);
                        if (m) {
                            if (m[1].length < m[2].length) {
                                host = m[1].trim();
                                title = m[2].trim();
                            } else {
                                title = m[1].trim();
                                host = m[2].trim();
                            }
                            break;
                        }
                    }

                    if (title === 'Ενεργό Space' || host === 'Άγνωστος') {
                        const rawLines = card.innerText
                            .split(/\n+/)
                            .map(l => l.trim())
                            .filter(l => l.length > 1);

                        const clean = rawLines.filter(l => !isNoiseLine(l));
                        const unique = [];
                        for (const l of clean) {
                            if (!unique.includes(l)) unique.push(l);
                        }

                        if (unique.length >= 2) {
                            host = unique[0];
                            title = unique[1];
                            if (unique[1].length < 6 && unique.length > 2 && unique[2].length > unique[1].length) {
                                title = unique[2];
                            }
                        } else if (unique.length === 1) {
                            title = unique[0];
                        }
                    }

                    if (host === 'Άγνωστος' || host.length < 2) {
                        const userLinks = card.querySelectorAll('a[href^="/"]');
                        for (const ul of userLinks) {
                            const href = (ul.getAttribute('href') || '').split('?')[0];
                            const um = href.match(/^\/([A-Za-z0-9_]{2,20})$/);
                            if (um && !['i', 'home', 'explore', 'search', 'settings', 'messages', 'notifications', 'compose'].includes(um[1])) {
                                host = '@' + um[1];
                                break;
                            }
                        }
                    }

                    host = host.replace(/\s*Διοργανωτής\s*/gi, '').replace(/\s*Host\s*/gi, '').trim() || 'Άγνωστος';
                }

                results.push({
                    spaceId,
                    title: (title || 'Ενεργό Space').substring(0, 140),
                    host: (host || 'Άγνωστος').substring(0, 80),
                    url: `https://x.com/i/spaces/${spaceId}`,
                    listeners
                });
            });

            return results;
        });

        console.log('Βρέθηκαν spaces:', spacesData.length, spacesData);
        await browser.close();
        browser = null;

        const now = new Date();
        const dateStr = now.toISOString().split('T')[0];
        const timeStr = now.toTimeString().split(' ')[0].substring(0, 5);

        const enrichedSpaces = await Promise.all(spacesData.map(async (s) => {
            return new Promise((resolve) => {
                const cmd = `.\\yt-dlp.exe --cookies "${COOKIES_PATH}" -g "${s.url}"`;
                exec(cmd, { timeout: 20000 }, (err, stdout) => {
                    resolve({
                        title: s.title,
                        date: dateStr,
                        time: timeStr,
                        spaceId: s.spaceId,
                        host: s.host,
                        url: s.url,
                        streamUrl: err ? null : (stdout || '').trim() || null,
                        listeners: s.listeners
                    });
                });
            });
        }));

        const result = saveUniqueSpaces(enrichedSpaces);
        lastScanTime = new Date().toISOString();
        lastScanResult = {
            success: true,
            addedCount: result.addedCount,
            total: result.spaces.length
        };

        console.log(`[Scan] Ολοκληρώθηκε στις ${timeStr}. Προστέθηκαν/ενημερώθηκαν: ${result.addedCount}`);

        // Spaces που μόλις τελείωσαν: ανάκτηση ομιλητών στο παρασκήνιο (δεν μπλοκάρει το scan cycle)
        if (result.justEnded && result.justEnded.length > 0) {
            console.log(`[Speakers] ${result.justEnded.length} space(s) μόλις τελείωσαν, ανάκτηση ομιλητών σε εξέλιξη...`);
            (async () => {
                for (const ended of result.justEnded) {
                    if (!ended.url) continue;
                    try {
                        const r = await scrapeSpeakersForUrl(ended.url);
                        if (r.success && r.speakers.length > 0) {
                            saveSpeakersToHistory(ended.spaceId, r.speakers);
                            console.log(`[Speakers] Αποθηκεύτηκαν ${r.speakers.length} ομιλητές για "${ended.title}"`);
                        }
                    } catch (err) {
                        console.error(`[Speakers] Σφάλμα για "${ended.title}":`, err.message);
                    }
                    await new Promise(r => setTimeout(r, 3000)); // μικρή παύση ανάμεσα σε διαδοχικά requests
                }
            })();
        }

        return { success: true, spaces: result.spaces, addedCount: result.addedCount };
    } catch (err) {
        if (browser) await browser.close();
        console.error('Scan error:', err);
        lastScanTime = new Date().toISOString();
        lastScanResult = { success: false, addedCount: 0, total: 0, error: err.message };
        return { success: false, error: 'Σφάλμα κατά τη σάρωση: ' + (err.message || 'Άγνωστο'), spaces: [], addedCount: 0 };
    } finally {
        isScanning = false;
    }
}

function startAutoScan() {
    if (autoScanTimer) clearInterval(autoScanTimer);
    autoScanTimer = setInterval(async () => {
        if (!autoScanEnabled) return;
        console.log(`[Auto-Scan] Ξεκινάει αυτόματη σάρωση... (${new Date().toLocaleTimeString()})`);
        await doScan();
    }, AUTO_SCAN_INTERVAL_MS);
    console.log(`Auto-scan ενεργό: κάθε ${AUTO_SCAN_INTERVAL_MS / 60000} λεπτά`);
}

function stopAutoScan() {
    if (autoScanTimer) {
        clearInterval(autoScanTimer);
        autoScanTimer = null;
    }
    console.log('Auto-scan απενεργοποιήθηκε');
}

// 1. Χειροκίνητη σάρωση
app.post('/scan-spacebar', async (req, res) => {
    const result = await doScan();
    res.json(result);
});

// 2. Προσθήκη μεμονωμένου URL
app.post('/add-by-url', (req, res) => {
    let { url } = req.body;
    if (!url) return res.json({ success: false, error: 'Δώστε URL.' });

    let match = url.match(/\/i\/spaces\/([a-zA-Z0-9]+)/);
    let spaceId = match ? match[1] : 'Unknown';

    const cmd = `.\\yt-dlp.exe --cookies "${COOKIES_PATH}" -g "${url}"`;
    exec(cmd, { timeout: 20000 }, (err, stdout) => {
        const now = new Date();
        const newSpace = {
            title: `Space #${spaceId}`,
            date: now.toISOString().split('T')[0],
            time: now.toTimeString().split(' ')[0].substring(0, 5),
            spaceId: spaceId,
            host: 'Unknown',
            url: url,
            streamUrl: err ? null : (stdout || '').trim() || null
        };
        const result = saveUniqueSpaces([newSpace]);
        res.json({ success: true, spaces: result.spaces, addedCount: result.addedCount });
    });
});

// 3. Ανάγνωση Ιστορικού
app.get('/get-history', (req, res) => {
    if (!fs.existsSync(HISTORY_FILE)) {
        fs.writeFileSync(HISTORY_FILE, JSON.stringify([], null, 2));
    }
    try {
        const data = fs.readFileSync(HISTORY_FILE, 'utf8');
        let parsed = JSON.parse(data);
        let spaces = Array.isArray(parsed) ? parsed : (parsed.spaces || []);
        res.json({ success: true, spaces: spaces });
    } catch (err) {
        res.json({ success: true, spaces: [] });
    }
});

// 3α2. Μαζική ανάκτηση stream URL για spaces που δεν το έχουν αποθηκευμένο
function execYtDlpGetUrl(spaceUrl) {
    return new Promise((resolve) => {
        const cmd = `.\\yt-dlp.exe --cookies "${COOKIES_PATH}" -g "${spaceUrl}"`;
        exec(cmd, { timeout: 20000 }, (err, stdout) => {
            const result = err ? null : (stdout || '').trim();
            resolve(result || null);
        });
    });
}

app.post('/retry-missing-streams', async (req, res) => {
    if (retryMissingStreamsRunning) {
        return res.json({ success: false, error: 'Ήδη τρέχει μια προηγούμενη ανάκτηση, περίμενε να τελειώσει.' });
    }
    retryMissingStreamsRunning = true;
    try {
        if (!fs.existsSync(HISTORY_FILE)) {
            return res.json({ success: true, attempted: 0, recovered: 0 });
        }
        let data = fs.readFileSync(HISTORY_FILE, 'utf8');
        let parsed = JSON.parse(data);
        let spaces = Array.isArray(parsed) ? parsed : (parsed.spaces || []);

        const targets = spaces.filter(s => !s.streamUrl && s.url);
        let recovered = 0;

        for (const target of targets) {
            const newStreamUrl = await execYtDlpGetUrl(target.url);
            if (newStreamUrl) {
                const idx = spaces.findIndex(s => s.spaceId === target.spaceId);
                if (idx !== -1) {
                    spaces[idx].streamUrl = newStreamUrl;
                    recovered++;
                }
            }
            // Μικρή παύση ανάμεσα σε διαδοχικά requests, να μην «χτυπάμε» απότομα το X
            await new Promise(r => setTimeout(r, 1500));
        }

        fs.writeFileSync(HISTORY_FILE, JSON.stringify(spaces, null, 2));
        res.json({ success: true, attempted: targets.length, recovered });
    } catch (err) {
        console.error('retry-missing-streams error:', err);
        res.json({ success: false, error: err.message });
    } finally {
        retryMissingStreamsRunning = false;
    }
});

// 3β. Πραγματικό download του ήχου μέσω yt-dlp (streaming, χωρίς προσωρινά αρχεία)
app.get('/download-space', (req, res) => {
    const { url, title, date, time, host } = req.query;
    if (!url) return res.status(400).send('Λείπει το url.');

    function sanitize(s){
        return (s || '')
            .toString()
            .replace(/[\\/:*?"<>|]/g, '')
            .trim();
    }

    const parts = [sanitize(date), sanitize(time), sanitize(host), sanitize(title)].filter(Boolean);
    const combined = (parts.length > 0 ? parts.join(' - ') : (sanitize(title) || 'space')).substring(0, 150) || 'space';

    // ASCII-only fallback filename (Content-Disposition header δέχεται μόνο ASCII στο βασικό filename=)
    const asciiFallback = combined
        .replace(/[^\x20-\x7E]/g, '') // αφαίρεση μη-ASCII (π.χ. Ελληνικά, emoji)
        .trim() || 'space';

    // Σωστό UTF-8 filename (RFC 5987) ώστε να εμφανίζεται σωστά ο πραγματικός τίτλος στον browser
    const encodedTitle = encodeURIComponent(combined + '.m4a');

    res.setHeader('Content-Disposition', `attachment; filename="${asciiFallback}.m4a"; filename*=UTF-8''${encodedTitle}`);
    res.setHeader('Content-Type', 'audio/mp4');

    const ytDlpPath = path.join(__dirname, 'yt-dlp.exe');
    const args = ['--cookies', COOKIES_PATH, '-f', 'bestaudio/best', '-o', '-', url];
    const proc = spawn(ytDlpPath, args);

    proc.stdout.pipe(res);

    proc.stderr.on('data', (chunk) => {
        process.stderr.write(chunk); // raw write, όχι console.error — κρατάει το \r ώστε η γραμμή να ενημερώνεται επί τόπου
    });

    proc.on('error', (err) => {
        console.error('yt-dlp spawn error:', err);
        if (!res.headersSent) res.status(500).send('Σφάλμα εκκίνησης yt-dlp.');
    });

    proc.on('close', (code) => {
        if (code !== 0) {
            console.error(`yt-dlp exited with code ${code} for url ${url}`);
        }
        // Ασφάλεια: αν το pipe δεν έκλεισε σωστά τη σύνδεση (π.χ. λόγω απότομου σφάλματος),
        // κλείνουμε ρητά ώστε ο browser να μην μείνει σε "Unconfirmed .crdownload".
        if (!res.writableEnded) {
            res.end();
        }
    });

    req.on('close', () => {
        if (!proc.killed) proc.kill();
    });
});

// 3γ. Κόψιμο συγκεκριμένου χρονικού διαστήματος (clip) μέσω ffmpeg — streaming, χωρίς προσωρινά αρχεία.
// Δουλεύει απευθείας πάνω στο direct CDN URL (m3u8), όχι μέσω yt-dlp, γιατί χρειαζόμαστε -ss/-to κοπή.
app.get('/download-clip', (req, res) => {
    const { url, start, end, title, date, time, host } = req.query;
    if (!url) return res.status(400).send('Λείπει το url.');
    if (start === undefined || end === undefined) return res.status(400).send('Λείπουν τα όρια κοπής (start/end).');

    function sanitize(s){
        return (s || '')
            .toString()
            .replace(/[\\/:*?"<>|]/g, '')
            .trim();
    }

    function formatClipTime(sec){
        const n = parseFloat(sec);
        if (isNaN(n)) return '';
        const h = Math.floor(n / 3600);
        const m = Math.floor((n % 3600) / 60);
        const s = Math.floor(n % 60);
        // Χρήση '-' αντί για ':' γιατί οι άνω-κάτω τελείες δεν επιτρέπονται σε ονόματα αρχείων στα Windows
        return h > 0
            ? `${h}-${String(m).padStart(2,'0')}-${String(s).padStart(2,'0')}`
            : `${m}-${String(s).padStart(2,'0')}`;
    }

    const clipRange = `(clip ${formatClipTime(start)} - ${formatClipTime(end)})`;

    const parts = [sanitize(date), sanitize(time), sanitize(host), sanitize(title)].filter(Boolean);
    const combined = (parts.length > 0 ? parts.join(' - ') : (sanitize(title) || 'clip')).substring(0, 150) || 'clip';
    const asciiFallback = combined.replace(/[^\x20-\x7E]/g, '').trim() || 'clip';
    const encodedTitle = encodeURIComponent(combined + ' ' + clipRange + '.aac');

    res.setHeader('Content-Disposition', `attachment; filename="${asciiFallback} ${clipRange}.aac"; filename*=UTF-8''${encodedTitle}`);
    res.setHeader('Content-Type', 'audio/aac');

    const startSec = parseFloat(start);
    const endSec = parseFloat(end);
    const durationSec = endSec - startSec;
    if (isNaN(startSec) || isNaN(endSec) || durationSec <= 0) {
        return res.status(400).send('Μη έγκυρα όρια κοπής.');
    }

    // -ss/-to ΜΕΤΑ το -i: ακριβές κόψιμο πάνω στο πραγματικό, σωστά αναλυμένο timeline του input.
    // (Είχαμε δοκιμάσει -ss ΠΡΙΝ το -i για ταχύτητα, αλλά σε πολύ μεγάλα spaces το γρήγορο HLS seek
    // μπορούσε να «προσπεράσει» λάθος σημείο και να καταλήξει σε άδειο αρχείο. Προτεραιότητα στη σιγουριά.)
    // Έξοδος σε raw ADTS AAC (.aac) αντί για mp4: το mp4/m4a container με fragmentation ήταν εύθραυστο
    // (προβλήματα ήχου σε ορισμένους players). Το ADTS παίζεται παντού και δεν χρειάζεται seek/moov.
    const args = [
        '-i', url,
        '-ss', String(startSec),
        '-to', String(endSec),
        '-c', 'copy',
        '-f', 'adts',
        'pipe:1'
    ];
    const proc = spawn('ffmpeg', args);

    proc.stdout.pipe(res);

    proc.stderr.on('data', (chunk) => {
        process.stderr.write(chunk); // raw write, όχι console.error — κρατάει το \r ώστε η γραμμή να ενημερώνεται επί τόπου
    });

    proc.on('error', (err) => {
        console.error('ffmpeg spawn error:', err);
        if (!res.headersSent) res.status(500).send('Σφάλμα εκκίνησης ffmpeg. Είναι εγκατεστημένο και στο PATH;');
    });

    proc.on('close', (code) => {
        if (code !== 0) {
            console.error(`ffmpeg exited with code ${code} for url ${url}`);
        }
        if (!res.writableEnded) {
            res.end();
        }
    });

    req.on('close', () => {
        if (!proc.killed) proc.kill();
    });
});

// Επαναχρησιμοποιήσιμη λογική: ανοίγει τη σελίδα ενός (τελειωμένου) space και εξάγει τους ομιλητές
async function scrapeSpeakersForUrl(url) {
    let browser;
    try {
        browser = await puppeteer.launch({
            headless: 'new',
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--window-size=1280,900'
            ]
        });

        const page = await browser.newPage();
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36');
        await page.setViewport({ width: 1280, height: 900 });

        await page.evaluateOnNewDocument(() => {
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
        });

        const cookies = parseNetscapeCookies(COOKIES_PATH);
        if (cookies.length > 0) {
            await page.setCookie(...cookies);
        }

        await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
        await new Promise(r => setTimeout(r, 4000));

        const speakers = await page.evaluate(() => {
            const results = [];
            const seenHandles = new Set();
            const roleRegex = /^(Διοργανωτής|Συνδιοργ\.?|Συνδιοργανωτής|Ομιλητές|Host|Co-hosts?|Speakers?)$/i;
            const cells = Array.from(document.querySelectorAll('li[data-testid="UserCell"]'));

            cells.forEach(cell => {
                const linkEl = cell.querySelector('a[href^="/"]');
                if (!linkEl) return;
                const href = (linkEl.getAttribute('href') || '').split('?')[0];
                const m = href.match(/^\/([A-Za-z0-9_]{2,20})$/);
                if (!m) return;
                const handle = '@' + m[1];
                if (seenHandles.has(handle)) return;

                // Το display name είναι μέσα στο πρώτο div[dir="ltr"] (το δεύτερο div[dir="ltr"] είναι το @handle).
                // Διαβάζουμε ΟΛΟ το textContent του, όχι μεμονωμένα spans, γιατί σε ονόματα με emoji/σύμβολα
                // το κείμενο συχνά είναι σπασμένο σε πολλαπλά φωλιασμένα spans.
                let name = '';
                const ltrDivs = linkEl.querySelectorAll('div[dir="ltr"]');
                if (ltrDivs.length > 0) {
                    const t = (ltrDivs[0].textContent || '').trim();
                    if (t && t !== handle) name = t;
                }
                if (!name) {
                    const spans = linkEl.querySelectorAll('span');
                    for (const s of spans) {
                        const t = (s.textContent || '').trim();
                        if (t && t.length > 0 && t !== handle) { name = t; break; }
                    }
                }

                let role = '';
                let node = cell;
                let hops = 0;
                outer:
                while (node && hops < 400) {
                    let sib = node.previousElementSibling;
                    while (sib) {
                        const t = (sib.textContent || '').trim();
                        if (roleRegex.test(t)) { role = t; break outer; }
                        sib = sib.previousElementSibling;
                        hops++;
                        if (hops > 400) break outer;
                    }
                    node = node.parentElement;
                    hops++;
                }

                seenHandles.add(handle);
                // Αν δεν βρέθηκε ετικέτα ρόλου (π.χ. λόγω virtualized λίστας), θεωρούμε πως είναι Ομιλητής —
                // ο Διοργανωτής/Συνδιοργανωτής είναι πάντα ελάχιστοι και στην αρχή, οπότε σπάνια χάνονται.
                results.push({ name: name || handle, handle, role: role || 'ΟΜΙΛΗΤΕΣ' });
            });

            return results;
        });

        await browser.close();
        browser = null;
        return { success: true, speakers };
    } catch (err) {
        if (browser) await browser.close();
        console.error('scrapeSpeakersForUrl error:', err);
        return { success: false, speakers: [], error: err.message };
    }
}

// Αποθηκεύει τους ομιλητές ενός συγκεκριμένου space μόνιμα στο history.json
function saveSpeakersToHistory(spaceId, speakers) {
    try {
        if (!fs.existsSync(HISTORY_FILE)) return;
        let data = fs.readFileSync(HISTORY_FILE, 'utf8');
        let parsed = JSON.parse(data);
        let spaces = Array.isArray(parsed) ? parsed : (parsed.spaces || []);
        const idx = spaces.findIndex(s => s.spaceId === spaceId);
        if (idx !== -1) {
            spaces[idx].speakers = speakers;
            spaces[idx].speakersFetchedAt = new Date().toISOString();
            fs.writeFileSync(HISTORY_FILE, JSON.stringify(spaces, null, 2));
        }
    } catch (err) {
        console.error('Σφάλμα αποθήκευσης ομιλητών:', err);
    }
}

// 3γ. Ανάκτηση ομιλητών ενός Space (on-demand από το UI) — αποθηκεύει και μόνιμα το αποτέλεσμα
app.get('/get-speakers', async (req, res) => {
    const { url, spaceId } = req.query;
    if (!url) return res.json({ success: false, error: 'Λείπει το url.' });

    const result = await scrapeSpeakersForUrl(url);
    if (result.success && spaceId && result.speakers.length > 0) {
        saveSpeakersToHistory(spaceId, result.speakers);
    }

    res.json({
        success: result.success,
        speakers: result.speakers,
        error: result.error,
        note: (result.success && result.speakers.length === 0) ? 'Δεν βρέθηκαν ομιλητές (πιθανόν το space δεν έχει τελειώσει ή δεν φορτώθηκε σωστά).' : undefined
    });
});

// 4. Σήμανση ότι διαβάστηκαν
app.post('/mark-read', (req, res) => {
    if (fs.existsSync(HISTORY_FILE)) {
        try {
            let data = fs.readFileSync(HISTORY_FILE, 'utf8');
            let parsed = JSON.parse(data);
            let spaces = Array.isArray(parsed) ? parsed : (parsed.spaces || []);
            spaces.forEach(s => s.isNew = false);
            fs.writeFileSync(HISTORY_FILE, JSON.stringify(spaces, null, 2));
            res.json({ success: true, spaces: spaces });
        } catch (err) {
            res.json({ success: false });
        }
    } else {
        res.json({ success: false });
    }
});

// 5. Διαγραφή Space
app.post('/delete-space', (req, res) => {
    const { spaceId } = req.body;
    if (fs.existsSync(HISTORY_FILE)) {
        try {
            let data = fs.readFileSync(HISTORY_FILE, 'utf8');
            let parsed = JSON.parse(data);
            let spaces = Array.isArray(parsed) ? parsed : (parsed.spaces || []);
            spaces = spaces.filter(s => s.spaceId !== spaceId);
            fs.writeFileSync(HISTORY_FILE, JSON.stringify(spaces, null, 2));
            res.json({ success: true, spaces: spaces });
        } catch (err) {
            res.json({ success: false, error: 'Σφάλμα κατά την επεξεργασία.' });
        }
    } else {
        res.json({ success: false, error: 'Το αρχείο δεν βρέθηκε.' });
    }
});

// 6. Auto-scan status & control
app.get('/auto-scan-status', (req, res) => {
    res.json({
        success: true,
        enabled: autoScanEnabled,
        intervalMinutes: AUTO_SCAN_INTERVAL_MS / 60000,
        isScanning,
        lastScanTime,
        lastScanResult
    });
});

app.post('/auto-scan-toggle', (req, res) => {
    const { enabled } = req.body;
    autoScanEnabled = !!enabled;

    if (autoScanEnabled) {
        startAutoScan();
    } else {
        stopAutoScan();
    }

    res.json({
        success: true,
        enabled: autoScanEnabled,
        intervalMinutes: AUTO_SCAN_INTERVAL_MS / 60000
    });
});

app.listen(PORT, () => {
    console.log(`Server running on http://localhost:${PORT}`);
    startAutoScan();
    // Πρώτη σάρωση μετά από 15 δευτερόλεπτα (να προλάβει να ανοίξει ο browser)
    setTimeout(() => {
        if (autoScanEnabled) {
            console.log('[Auto-Scan] Πρώτη αυτόματη σάρωση...');
            doScan();
        }
    }, 15000);
});
