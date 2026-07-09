const http = require('http');
const BASE = 'http://localhost:3000';
const AUTH = { 'Authorization': 'Bearer fxmozo123', 'Content-Type': 'application/json' };
function get(e) { return new Promise((r,j) => { http.get(BASE+e,{headers:AUTH},res=>{let d='';res.on('data',c=>d+=c);res.on('end',()=>{try{r(JSON.parse(d))}catch(e){j(e)}})}).on('error',j) }); }
function post(e,d) { return new Promise((r,j) => { const b=JSON.stringify(d); const req=http.request(BASE+e,{method:'POST',headers:{...AUTH,'Content-Length':Buffer.byteLength(b)}},res=>{let d='';res.on('data',c=>d+=c);res.on('end',()=>{try{r(JSON.parse(d))}catch(e){j(e)}})}); req.on('error',j); req.write(b); req.end(); }); }
async function main() {
  const c = await get('/api/admin/prop-firm-data');

  // Update PFP price labels
  c.pfpPriceLabels = [
    { key: 'accessFeeLabel', value: 'Access Fee', description: 'One-time fee to access and start the prop firm challenge' },
    { key: 'initialFeeLabel', value: 'Initial Fee', description: 'Initial upfront payment required to begin' },
    { key: 'fundedFeeLabel', value: 'After Evaluation Funded Fee From Profit', description: 'Fee deducted from your first profit payout after successfully passing the evaluation' }
  ];

  // Remove legacy classicPrices — classicBeePrices is sufficient
  delete c.classicPrices;

  // Set buy price for Level 1 ($10k) Instant Growth
  if (c.instantLevels && c.instantLevels['1']) {
    c.instantLevels['1'].buyPrice = 299;
  }

  // Set 2-Step Classic Bee Prices (leverage-tier pricing per account size)
  c.classicBeePrices = {
    "2000":   { "NewBee": 15,   "WorkerBee": 19,   "QueenBee": 29 },
    "5000":   { "NewBee": 29,   "WorkerBee": 39,   "QueenBee": 49 },
    "10000":  { "NewBee": 79,   "WorkerBee": 99,   "QueenBee": 119 },
    "25000":  { "NewBee": 149,  "WorkerBee": 199,  "QueenBee": 229 },
    "50000":  { "NewBee": 299,  "WorkerBee": 349,  "QueenBee": 399 },
    "100000": { "NewBee": 499,  "WorkerBee": 599,  "QueenBee": 699 },
    "200000": { "NewBee": 899,  "WorkerBee": 1099, "QueenBee": 1199 }
  };

  const r = await post('/api/admin/prop-firm-data', c);
  console.log('Save:', JSON.stringify(r));

  const v = await get('/api/admin/prop-firm-data');
  console.log('pfpPriceLabels:', JSON.stringify(v.pfpPriceLabels));
  console.log('classicBeePrices:', JSON.stringify(v.classicBeePrices));
  console.log('Done');
}
main().catch(e => console.error(e));
