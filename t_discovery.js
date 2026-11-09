/* اختبار 3.69 — اكتشاف «كل الوحدات (4 وحدات) في دفعة واحدة» بلا جهاز.
 *
 * يشغّل coursera.js داخل Node مع محاكاة واجهات Coursera:
 *   • 4 وحدات × (2–3) محاضرات ⇒ كلها في نتيجة واحدة عند scope='course'
 *   • كل صف يحمل: اسم الوحدة، الجودات المتاحة، ورابط الترجمة العربية
 *   • التأكد أن scope='module' ما زال يعمل وأن نطاق الوحدة الواحدة لم يتغيّر
 *
 * التشغيل: node t_discovery.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS_PATH = path.join(__dirname, '..', 'NoteWorm-v3.14-server', 'app',
  'src', 'main', 'assets', 'coursera.js');

const MODULES = ['الوحدة 1 — الأساسيات', 'الوحدة 2 — الانحدار',
  'الوحدة 3 — التصنيف', 'الوحدة 4 — الشبكات'];
const PER_MODULE = [3, 2, 3, 2];
const TOTAL = PER_MODULE.reduce((a, b) => a + b, 0);

function materialsPayload() {
  const modules = [], lessons = [], items = [];
  MODULES.forEach((name, mi) => {
    const mid = 'm' + (mi + 1);
    const lids = [];
    for (let k = 0; k < PER_MODULE[mi]; k++) {
      const lid = 'l' + (mi + 1) + '_' + k;
      lids.push(lid);
      const iid = 'i' + (mi + 1) + '_' + k;
      lessons.push({ id: lid, name: 'درس', itemIds: [iid], elementIds: [] });
      items.push({
        id: iid, name: name + ' - محاضرة ' + (k + 1),
        contentSummary: { typeName: 'lecture' },
        moduleId: mid, lessonId: lid, isLocked: false,
      });
    }
    modules.push({ id: mid, name, slug: mid, lessonIds: lids });
  });
  return {
    elements: [{ id: 'c1', courseId: 'c1', name: 'دورة تجريبية',
      moduleIds: modules.map(m => m.id) }],
    linked: {
      'onDemandCourseMaterialModules.v1': modules,
      'onDemandCourseMaterialLessons.v1': lessons,
      'onDemandCourseMaterialItems.v2': items,
    },
  };
}

function videoPayload(id, withArabic = true) {
  const byResolution = {
    '1080p': { mp4VideoUrl: 'https://d3c33hcgiwev3.cloudfront.net/v1080.mp4' },
    '720p': { mp4VideoUrl: 'https://d3c33hcgiwev3.cloudfront.net/v720.mp4' },
    '480p': { mp4VideoUrl: 'https://d3c33hcgiwev3.cloudfront.net/v480.mp4' },
  };
  const vtt = withArabic
    ? { ar: 'https://d3c33hcgiwev3.cloudfront.net/' + id + '.ar.vtt' } : {};
  return { linked: { 'onDemandVideos.v1': [{ sources: { byResolution }, subtitlesVtt: vtt }] } };
}

function makeContext() {
  const calls = [];
  const fetch = async (url) => {
    calls.push(String(url));
    let body;
    if (String(url).startsWith('/api/onDemandCourseMaterials')) {
      body = materialsPayload();
    } else if (String(url).startsWith('/api/onDemandLectureVideos')) {
      const id = decodeURIComponent(String(url).split('/').pop().split('?')[0]).split('~').pop();
      // محاضرة واحدة بلا ترجمة عربية لاختبار التصفية
      body = videoPayload(id, id !== 'i4_1');
    } else {
      throw new Error('مسار غير متوقع في الاختبار: ' + url);
    }
    return { ok: true, status: 200, json: async () => body };
  };
  const ctx = {
    window: {}, console,
    location: { hostname: 'www.coursera.org', origin: 'https://www.coursera.org',
      pathname: '/learn/ml/lecture/abc', search: '', href: 'https://www.coursera.org/learn/ml' },
    document: { querySelectorAll: () => [], querySelector: () => null },
    fetch, AbortController, setTimeout, clearTimeout, URL, decodeURIComponent, encodeURIComponent,
    Object, Array, String, Number, Boolean, JSON, Math, Error, Promise, RegExp, Date,
  };
  ctx.window = ctx;                 // النافذة هي السياق نفسه (window.__nwcs)
  ctx.globalThis = ctx;
  return { ctx, calls };
}

async function runDiscovery(js, scope, moduleId) {
  const { ctx, calls } = makeContext();
  vm.createContext(ctx);
  vm.runInContext(js, ctx, { filename: 'coursera.js' });
  const api = ctx.window.NoteWormCoursera;
  if (!api || typeof api.run !== 'function') throw new Error('coursera.js لم يعرّف NoteWormCoursera.run');
  api.run(scope, moduleId || '', 'tok-' + scope, 'mp4', 'ml');
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const st = ctx.window.__nwcs;
    if (st && st.done) {
      if (st.error) throw new Error('الاكتشاف رفض: ' + st.error);
      return { result: st.result, calls };
    }
    await new Promise(r => setTimeout(r, 20));
  }
  throw new Error('انتهت المهلة بلا نتيجة اكتشاف');
}

(async () => {
  const js = fs.readFileSync(JS_PATH, 'utf8');

  // ① كل الوحدات دفعة واحدة
  const { result: all, calls } = await runDiscovery(js, 'course');
  console.log('① النطاق:', all.module, '· الصفوف:', all.rows.length);
  if (all.rows.length !== TOTAL) throw new Error(`عدد الصفوف ${all.rows.length} ≠ ${TOTAL}`);
  if (all.moduleId !== 'all') throw new Error('moduleId المتوقع all: ' + all.moduleId);

  const moduleNames = new Set(all.rows.map(r => r.moduleName));
  console.log('   الوحدات في نتيجة واحدة:', moduleNames.size, [...moduleNames].join(' | '));
  if (moduleNames.size !== 4) throw new Error('لم تُجمع الوحدات الأربع');
  if (![...moduleNames].every(n => MODULES.includes(n))) throw new Error('أسماء وحدات غير متوقعة');

  const ready = all.rows.filter(r => r.status === 'ready');
  const withSub = all.rows.filter(r => r.subtitle && r.subtitle.url);
  const qualities = new Set();
  all.rows.forEach(r => (r.qualities || []).forEach(q => qualities.add(q.id)));
  console.log('   جاهزة:', ready.length, '· بترجمة عربية:', withSub.length,
    '· الجودات:', [...qualities].sort().reverse().join(', '));
  if (ready.length !== TOTAL) throw new Error('صفوف غير جاهزة: ' + all.rows.map(r => r.status));
  if (withSub.length !== TOTAL - 1) throw new Error('عدد الترجمات غير متوقع: ' + withSub.length);
  if ([...qualities].sort().join(',') !== '1080p,480p,720p') throw new Error('الجودات: ' + [...qualities]);
  if (!all.rows.every(r => r.moduleIndex >= 1 && r.moduleIndex <= 4)) throw new Error('moduleIndex ناقص');

  const noSub = all.rows.find(r => !r.subtitle);
  console.log('   المحاضرة بلا ترجمة (لازم تتنزّل فيديو فقط):', noSub.title);
  if (noSub.moduleName !== MODULES[3]) throw new Error('حالة غياب الترجمة تغيّرت: ' + noSub.title);
  if (noSub.status !== 'ready') throw new Error('الصف بلا ترجمة لازم يكون جاهزًا للفيديو فقط');

  // يبدأ صفًا لكل محاضرة ⇒ طلب واحد لكل محاضرة
  const videoCalls = calls.filter(c => c.startsWith('/api/onDemandLectureVideos'));
  if (videoCalls.length !== TOTAL) throw new Error('عدد طلبات الفيديو ' + videoCalls.length);
  console.log('   طلبات الفيديو:', videoCalls.length, '(محاضرة واحدة لكل طلب)');

  // ② نطاق الوحدة الواحدة لم يتغيّر
  const { result: one } = await runDiscovery(js, 'module', 'm2');
  console.log('② وحدة واحدة:', one.module, '· الصفوف:', one.rows.length);
  if (one.rows.length !== PER_MODULE[1]) throw new Error('عدد صفوف الوحدة تغيّر: ' + one.rows.length);

  // ③ سرد الوحدات (بلا moduleId) كما كان
  const { result: list } = await runDiscovery(js, 'module', '');
  console.log('③ سرد الوحدات:', list.kind, list.modules.map(m => m.name + '/' + m.count).join(' | '));
  if (list.kind !== 'modules' || list.modules.length !== 4) throw new Error('سرد الوحدات تغيّر');

  console.log('\nكل اختبارات اكتشاف «كل الوحدات + الجودات + الترجمات» نجحت ✅');
})().catch(err => { console.error('❌', err.message); process.exit(1); });
