import {build} from 'esbuild';
import {readFile,writeFile,copyFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const root = fileURLToPath(new URL('../', import.meta.url));
await build({
  entryPoints:[root+'frontend/ring.jsx'], outfile:root+'src/gas_web/static/ring.js',
  bundle:true, minify:true, format:'esm', target:['chrome110','firefox115','safari16.4'],
  loader:{'.woff2':'file','.woff':'file'}, assetNames:'[name]-[hash]',
  define:{'process.env.NODE_ENV':'"production"'}, legalComments:'linked',
});
const pkg = JSON.parse(await readFile(root+'frontend/package.json','utf8'));
await writeFile(root+'src/gas_web/static/ui-build.json',JSON.stringify({framework:'JetBrains Ring UI',dependencies:pkg.dependencies},null,2)+'\n');
await copyFile(root+'frontend/node_modules/@fontsource/inter/LICENSE',root+'src/gas_web/static/Inter-LICENSE.txt');
await copyFile(root+'frontend/node_modules/@jetbrains/ring-ui-built/LICENSE.txt',root+'src/gas_web/static/RingUI-LICENSE.txt');
