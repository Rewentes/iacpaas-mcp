import {build} from 'esbuild';
import {readFile,writeFile,copyFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
const root = fileURLToPath(new URL('../', import.meta.url));
await build({
  entryPoints:[root+'frontend/ring.jsx'], outfile:root+'src/gas_web/static/ring.js',
  bundle:true, minify:true, format:'esm', target:['chrome110','firefox115','safari16.4'],
  loader:{'.woff2':'file','.woff':'file'}, assetNames:'[name]-[hash]',
  define:{'process.env.NODE_ENV':'"production"'}, legalComments:'linked',
});
const appPath = root+'src/gas_web/static/app.js';
let appSource = await readFile(appPath,'utf8');
const imports = [...appSource.matchAll(/from '(\.\/([a-z_]+\.js))(?:\?v=[a-f0-9]+)?'/g)];
for (const match of imports) {
  const hash = createHash('sha256').update(await readFile(root+'src/gas_web/static/'+match[2])).digest('hex').slice(0,12);
  appSource = appSource.replace(match[0], `from '${match[1]}?v=${hash}'`);
}
await writeFile(appPath, appSource);
const appHash = createHash('sha256').update(appSource).digest('hex').slice(0,12);
const indexPath = root+'src/gas_web/static/index.html';
await writeFile(indexPath, (await readFile(indexPath,'utf8')).replace(/src="\/static\/app\.js(?:\?v=[a-f0-9]+)?"/, `src="/static/app.js?v=${appHash}"`));
const pkg = JSON.parse(await readFile(root+'frontend/package.json','utf8'));
await writeFile(root+'src/gas_web/static/ui-build.json',JSON.stringify({framework:'JetBrains Ring UI',dependencies:pkg.dependencies},null,2)+'\n');
await copyFile(root+'frontend/node_modules/@fontsource/inter/LICENSE',root+'src/gas_web/static/Inter-LICENSE.txt');
await copyFile(root+'frontend/node_modules/@jetbrains/ring-ui-built/LICENSE.txt',root+'src/gas_web/static/RingUI-LICENSE.txt');
