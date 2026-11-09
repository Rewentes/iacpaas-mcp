// A deliberately small Markdown subset, built with DOM nodes; raw HTML is text.
export function renderMarkdown(text, onCitation = null) {
  const root = document.createElement('div');
  root.className = 'message-content markdown';
  const inline = (node, value) => {
    const pattern = /(\[e\d[^\]\n]{0,255}\]|\*\*[^*\n]+\*\*|`[^`\n]+`)/g;
    let start = 0;
    for (const match of value.matchAll(pattern)) {
      node.append(document.createTextNode(value.slice(start, match.index)));
      const token = match[0];
      const tag = token.startsWith('**') ? 'strong' : token.startsWith('`') ? 'code' : onCitation ? 'button' : 'span';
      const child = document.createElement(tag);
      child.textContent = tag === 'strong' ? token.slice(2, -2) : tag === 'code' ? token.slice(1, -1) : token;
      if (tag === 'button') {
        child.type = 'button'; child.className = 'citation';
        child.setAttribute('aria-label', `Открыть источник ${token.slice(1, -1)}`);
        child.addEventListener('click', () => onCitation(token.match(/e\d+/)[0]));
      }
      node.append(child); start = match.index + token.length;
    }
    node.append(document.createTextNode(value.slice(start)));
  };
  let paragraph = null, list = null, code = null;
  const lines = String(text).split('\n');
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (line.trimStart().startsWith('```')) {
      paragraph = list = null;
      if (code) { code = null; continue; }
      const pre = document.createElement('pre'); code = document.createElement('code');
      pre.append(code); root.append(pre); continue;
    }
    if (code) { code.textContent += line + '\n'; continue; }
    if (!line.trim()) { paragraph = list = null; continue; }
    if (line.includes('|') && /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1] || '')) {
      paragraph = list = null;
      const wrap = document.createElement('div'); wrap.className = 'table-scroll';
      const table = document.createElement('table'), head = document.createElement('thead'), body = document.createElement('tbody');
      const cells = value => value.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
      const row = (value, tag) => { const tr = document.createElement('tr'); for (const cell of cells(value)) { const td = document.createElement(tag); inline(td, cell); tr.append(td); } return tr; };
      head.append(row(line, 'th')); index++;
      while (index + 1 < lines.length && lines[index + 1].trim() && lines[index + 1].includes('|')) { index++; body.append(row(lines[index], 'td')); }
      table.append(head, body); wrap.append(table); root.append(wrap); continue;
    }
    const item = line.match(/^\s*(?:([-*])\s+|\d+[.)]\s+)(.*)$/);
    if (item) {
      paragraph = null;
      const tag = item[1] ? 'ul' : 'ol';
      if (!list || list.tagName.toLowerCase() !== tag) { list = document.createElement(tag); root.append(list); }
      const li = document.createElement('li'); inline(li, item[2]); list.append(li); continue;
    }
    list = null;
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    if (heading) {
      paragraph = null;
      const title = document.createElement('h3'); inline(title, heading[1]); root.append(title); continue;
    }
    if (!paragraph) { paragraph = document.createElement('p'); root.append(paragraph); }
    else paragraph.append(document.createTextNode('\n'));
    inline(paragraph, line);
  }
  return root;
}
