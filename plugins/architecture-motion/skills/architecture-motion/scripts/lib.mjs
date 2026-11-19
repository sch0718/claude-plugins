// 점검 공통: 이 PC 의 Chrome 을 헤드리스로 띄워 뷰어를 캡처 모드(?export=1 — 토글·재생 막대 숨김)로 연다
import puppeteer from 'puppeteer-core';
import { existsSync, statSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const CANDIDATES = [
  process.env.CHROME,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
].filter(Boolean);

export function findChrome() {
  const c = CANDIDATES.find(p => existsSync(p));
  if (!c) throw new Error('Chrome 을 찾지 못했습니다. CHROME 환경변수로 실행 파일 경로를 주세요');
  return c;
}

export function viewerUrl(target, params) {
  let file = path.resolve(target);
  if (existsSync(file) && statSync(file).isDirectory()) file = path.join(file, 'index.html');
  if (!existsSync(file)) throw new Error('뷰어를 찾지 못했습니다: ' + file);
  return pathToFileURL(file).href + '?' + new URLSearchParams(params).toString();
}

export async function openViewer(target, params, size = { width: 1920, height: 1080 }) {
  const browser = await puppeteer.launch({
    executablePath: findChrome(), headless: true,
    args: ['--force-color-profile=srgb', '--hide-scrollbars', '--allow-file-access-from-files'],
  });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('requestfailed', r => errors.push('불러오기 실패: ' + r.url()));
  await page.setViewport({ ...size, deviceScaleFactor: 1 });
  await page.goto(viewerUrl(target, { export: '1', play: '0', ...params }), { waitUntil: 'load' });
  await page.evaluate(() => document.fonts.ready);
  return { browser, page, errors };
}
