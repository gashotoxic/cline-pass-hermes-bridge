const d = JSON.parse(require('fs').readFileSync('C:/Users/Gasho/.pi/agent/models.json', 'utf8'));
for (const prov of ['cline-bridge', 'cline-pass']) {
  const arr = (d.providers[prov] && d.providers[prov].models) || [];
  const m = arr.find(x => x.id === 'cline-pass/kimi-k3');
  console.log(prov + '.cline-pass/kimi-k3 contextWindow:', m ? m.contextWindow : '(absent)');
}
const ob = JSON.parse(require('fs').readFileSync('C:/Users/Gasho/.config/opencode/opencode.json', 'utf8'));
const k = ((ob.provider['cline-bridge'] || {}).models) || {};
console.log('opencode kimi-k3:', k['cline-pass/kimi-k3'] ? JSON.stringify(k['cline-pass/kimi-k3']) : '(absent)');
