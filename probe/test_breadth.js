// 市场广度实时层（M2：雷达直连东财）专项测试
// 覆盖：① mergeThermo 实时优先 + 降级 ② renderSectorLive 实时渲染
//       ③ renderBreadth 全字段渲染 + 缺字段不崩 ④ fetchBreadth 解析（mock 东财/新浪）
const fs = require('fs');
const vm = require('vm');
const RADAR = 'C:/Users/seon/WorkBuddy/2026-09-01-02-51-51/market-thermo-deploy/radar.html';
const code = /<script[^>]*>([\s\S]*?)<\/script>/.exec(fs.readFileSync(RADAR, 'utf8'))[1];
function el(id){ return { id, innerHTML:'', textContent:'', value:'', className:'', style:{}, addEventListener(){}, removeEventListener(){}, appendChild(){}, removeChild(){}, focus(){}, click(){} }; }
const els = {};
global.window = global;
global.document = {
  getElementById(id){ if(!els[id]) els[id]=el(id); return els[id]; },
  querySelectorAll(){ return { forEach(){}, length:0 }; }, querySelector(){ return null; },
  createElement(){ return el('c'); }, getElementsByTagName(){ return []; }, addEventListener(){},
  body:{ appendChild(){}, removeChild(){} }
};
global.localStorage = { s:{}, getItem(k){ return this.s[k] ?? null; }, setItem(k,v){ this.s[k]=String(v); }, removeItem(k){ delete this.s[k]; } };
global.Notification = function(){}; global.Notification.permission = 'granted';
global.navigator = { userAgent: 'node' };
global.location = { search: '' };
vm.runInThisContext(code, { filename: 'radar-inline.js' });

const A = [];
const ck = (n, c) => A.push((c ? '✅' : '❌') + ' ' + n);

// ============ ① mergeThermo：实时优先 + 降级 ============
(function(){
  const dj = { generated_at:'2026-09-03 10:25:39 GMT+8',
    margin:{date:'2026-09-02', chg5:0.25, chg10:-0.38},
    sector:{ top:[{name:'快照行业',flow:1,pct:0.1}], bottom:[] },
    futures_basis:{ items:[], avg_basis_pct:-0.7, attitude:'中性' } };
  const br = { sector:{ top:[{name:'实时行业',flow:5,pct:1.2}], bottom:[{name:'弱行业',flow:-2,pct:-1}] },
    futures_basis:{ items:[{name:'沪深300',basis:4.5,basis_pct:0.12}], avg_basis_pct:0.12, attitude:'主力偏多' } };

  const m1 = mergeThermo(dj, br);
  ck('实时有 sector/futures → 取实时（不取快照）',
    m1.sector.top[0].name==='实时行业' && m1.futures_basis.avg_basis_pct===0.12);
  ck('margin 仅来自 data.json(T-1)，实时层不提供', m1.margin && m1.margin.chg5===0.25);
  ck('_live 标记为真', m1._live === true);

  const m2 = mergeThermo(dj, null);
  ck('实时层为 null → sector/futures 降级到 data.json 快照',
    m2.sector && m2.sector.top[0].name==='快照行业' && m2.futures_basis.avg_basis_pct===-0.7);

  const m3 = mergeThermo(null, null);
  ck('两者皆 null → margin/sector/futures 全 null 且不崩',
    m3.margin===null && m3.sector===null && m3.futures_basis===null && m3._live===false);

  const br2 = { sector:{ top:[{name:'仅行业',flow:3,pct:1}], bottom:[] } };  // 有行业无期指
  const m4 = mergeThermo(dj, br2);
  ck('实时只有行业（无期指）→ 行业取实时、期指降级快照',
    m4.sector.top[0].name==='仅行业' && m4.futures_basis.avg_basis_pct===-0.7);
})();

// ============ ② renderSectorLive：实时渲染 + 负值不出现 "+-" ============
(function(){
  renderSectorLive({ top:[{name:'半导体',flow:12.3,pct:2.1},{name:'银行',flow:0.5,pct:0.3}],
                     bottom:[{name:'白酒',flow:-3.2,pct:-1.2}] });
  const h = els['sectorFlow'].innerHTML;
  ck('renderSectorLive 渲染 top 流入带正号', h.indexOf('半导体 +12.30亿')>=0);
  ck('renderSectorLive 渲染 bottom 负值不带 "+-"（应为 -3.20亿）', h.indexOf('-3.20亿')>=0 && h.indexOf('+-')<0);
  ck('renderSectorLive 标注实时口径', h.indexOf('实时口径')>=0);

  renderSectorLive({ top:[], bottom:[] });  // 空列表不崩
  ck('renderSectorLive 空数据不崩', true);
})();

// ============ ③ renderBreadth：全字段 + 缺字段不崩 ============
(function(){
  renderBreadth({ ts:new Date(),
    breadth:{ up:2000, down:1000, flat:300 },
    flow:{ main_net:2.0, xlarge_net:1.0, large_net:0.5, mid_net:0.3, small_net:0.2 },
    indices:{ '1.000001':{name:'上证',price:3200,pct:0.5,change:16},
              '0.399001':{name:'深证',price:10500,pct:-0.3,change:-30},
              '0.399006':{name:'创业板',price:2100,pct:1.2,change:25},
              '1.000688':{name:'科创50',price:950,pct:-0.8,change:-8} },
    sector:{ top:[{name:'半导体',flow:12.3,pct:2.1}], bottom:[{name:'白酒',flow:-3.2,pct:-1.2}] } });
  ck('renderBreadth 涨跌家数正确', els['brBreadth'].textContent === '2000 / 1000 / 300');
  ck('renderBreadth 上涨占比正确 (2000/3300=60.6%)', els['brAdv'].textContent === '60.6%');
  ck('renderBreadth 主力净流入带符号 class', els['brMain'].innerHTML.indexOf('+2.0 亿')>=0);
  ck('renderBreadth 渲染 4 个指数', (els['brIdx'].innerHTML.match(/<b /g)||[]).length === 4);
  ck('renderBreadth 行业流含实时行业名', els['brSector'].innerHTML.indexOf('半导体')>=0);

  // 缺字段不崩（先复位再验证「缺失字段不被改写」）
  els['brBreadth'].textContent = '—';
  renderBreadth({ ts:new Date() });
  ck('renderBreadth 仅 ts → 缺失字段保持 — 且不抛错', els['brBreadth'].textContent === '—');
  ck('renderBreadth null → 直接 return 不抛错', (renderBreadth(null), true));
})();

// ============ ④ fetchBreadth 解析（mock 东财 ulist/clist + 新浪期货）============
(async function(){
  // 覆盖全局 jsonpCb / sinaFutures，模拟东财实时响应
  global.jsonpCb = (url) => {
    if(url.indexOf('clist') >= 0){                  // 行业资金流（唯一的 clist 调用）
      return Promise.resolve({ data:{ diff:[
        {f14:'半导体',f62:1.2e8,f3:2.1},{f14:'白酒',f62:-3.0e8,f3:-1.2},{f14:'银行',f62:0.5e8,f3:0.3}
      ]}});
    }
    if(url.indexOf('f104') >= 0){                    // 涨跌家数 + 沪深主力净流入
      return Promise.resolve({ data:{ diff:[
        {f12:'1.000001',f14:'上证',f62:5e8,f66:1e8,f72:2e8,f78:1e8,f84:1e8,f104:2000,f105:1000,f106:300},
        {f12:'0.399001',f14:'深证',f62:-3e8,f66:0,f72:0,f78:0,f84:0,f104:0,f105:0,f106:0}
      ]}});
    }
    if(url.indexOf('1.000300,1.000905,1.000016,1.000852') >= 0){  // 期指现货(指数)
      return Promise.resolve({ data:{ diff:[
        {f12:'000300',f2:3900.5},{f12:'000905',f2:5800.2},{f12:'000016',f2:2650.1},{f12:'000852',f2:6200.3}
      ]}});
    }
    return Promise.resolve({ data:{ diff:[               // 主要指数
      {f12:'1.000001',f14:'上证指数',f2:3200,f3:0.5,f4:16},
      {f12:'0.399001',f14:'深证成指',f2:10500,f3:-0.3,f4:-30}
    ]}});
  };
  global.sinaFutures = () => Promise.resolve({ IF0:3905.0, IC0:5805.0, IH0:2655.0, IM0:6205.0 });

  const rt = await fetchBreadth();
  ck('fetchBreadth 解析行业流 top[0]=半导体', rt.sector && rt.sector.top[0].name==='半导体' && rt.sector.top[0].flow===1.2);
  ck('fetchBreadth 解析涨跌家数 up=2000/down=1000', rt.breadth.up===2000 && rt.breadth.down===1000);
  ck('fetchBreadth 解析主力净流入 (5e8-3e8)/1e8 = 2.0 亿', rt.flow && rt.flow.main_net===2.0);
  ck('fetchBreadth 解析指数 上证 pct=0.5', rt.indices && rt.indices['1.000001'].pct===0.5);
  ck('fetchBreadth 计算期指基差 IF0: (3905-3900.5)/3900.5≈0.115% → 升水',
    rt.futures_basis && rt.futures_basis.items.length===4 && rt.futures_basis.items[0].basis_pct>0.10);
  ck('fetchBreadth 计算四大期指平均基差', typeof rt.futures_basis.avg_basis_pct === 'number');

  // 还原（避免影响其它异步）
  delete global.jsonpCb; delete global.sinaFutures;

  const fails = A.filter(x => x[0] === '❌');
  console.log(A.join('\n'));
  console.log('\n' + (fails.length ? '❌ FAIL ' + fails.length + '/' + A.length : 'ALL_PASS (' + A.length + ')'));
  process.exit(fails.length ? 1 : 0);
})();
