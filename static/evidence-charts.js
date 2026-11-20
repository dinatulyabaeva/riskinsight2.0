'use strict';

// The server supplies values copied from successful MCP results, never chart code.
// This renderer accepts only a small data contract and creates safe DOM/SVG nodes.
window.RiskEvidenceCharts = (() => {
  const svgNS = 'http://www.w3.org/2000/svg';
  const palette = ['#6be3bd', '#a9b5ff', '#eabc77', '#82b9dc', '#ed9295', '#c7d888'];
  const units = {ratio: '%', rub: '₽', pp: 'п.п.', count: 'шт.'};
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function svgNode(tag, attrs = {}, text) {
    const el = document.createElementNS(svgNS, tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function format(value, unit, compact = false) {
    if (!finite(value)) return 'Нет данных';
    const shown = unit === 'ratio' ? value * 100 : value;
    return shown.toLocaleString('ru-RU', {
      minimumFractionDigits: compact ? 0 : 2,
      maximumFractionDigits: !compact && ['ratio', 'pp'].includes(unit) ? 6 : 2,
      ...(compact && unit === 'rub' ? {notation: 'compact'} : {}),
    }) + (units[unit] ? ' ' + units[unit] : '');
  }
  function normalized(raw) {
    if (!raw || !['line', 'bar'].includes(raw.kind) || !Object.hasOwn(units, raw.unit)) return null;
    if (!Array.isArray(raw.series) || !raw.series.length || raw.series.length > 8) return null;
    const series = raw.series.map(s => ({
      name: typeof s.name === 'string' ? s.name : 'Значение',
      points: Array.isArray(s.points) ? s.points.slice(0, 200).map(p => ({
        label: String(p.label ?? ''),
        value: finite(p.value) ? p.value : null,
      })) : [],
    }));
    return {...raw, series};
  }
  function lineChart(chart) {
    const labels = [...new Set(chart.series.flatMap(s => s.points.map(p => p.label)))];
    const values = chart.series.flatMap(s => s.points.map(p => p.value)).filter(finite);
    const width = 600, height = 275;
    const pad = {left: 80, right: 22, top: 22, bottom: 56};
    const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': chart.title});
    svg.append(svgNode('title', {}, chart.title));
    svg.append(svgNode('desc', {}, 'Точные значения и источники приведены в таблице под графиком. Пропуски не соединяются линией.'));
    const low = Math.min(0, ...values), max = Math.max(0, ...values);
    const high = max === low ? low + 1 : max + (max - low) * .12;
    const x = i => pad.left + i * (width - pad.left - pad.right) / Math.max(1, labels.length - 1);
    const y = v => height - pad.bottom - (v - low) / (high - low) * (height - pad.top - pad.bottom);
    for (let i = 0; i <= 4; i++) {
      const value = low + (high - low) * i / 4;
      svg.append(svgNode('line', {x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value), stroke: '#30414f', 'stroke-dasharray': '3 5'}));
      svg.append(svgNode('text', {x: pad.left - 9, y: y(value) + 4, fill: '#a6b7c5', 'text-anchor': 'end', 'font-size': 11}, format(value, chart.unit, true)));
    }
    labels.forEach((label, i) => {
      if (labels.length <= 7 || i === 0 || i === labels.length - 1 || i % Math.ceil(labels.length / 6) === 0) {
        const shown = /^\d{4}-\d{2}-\d{2}$/.test(label) ? label.slice(0, 7) : label.slice(0, 18);
        svg.append(svgNode('text', {x: x(i), y: height - 28, fill: '#a6b7c5', 'text-anchor': 'middle', 'font-size': 10}, shown));
      }
    });
    chart.series.forEach((series, index) => {
      const byLabel = new Map(series.points.map(p => [p.label, p.value]));
      const color = palette[index % palette.length];
      let path = '', connected = false;
      labels.forEach((label, i) => {
        const value = byLabel.get(label);
        if (!finite(value)) { connected = false; return; }
        path += `${connected ? 'L' : 'M'}${x(i)},${y(value)} `;
        connected = true;
        const dot = svgNode('circle', {cx: x(i), cy: y(value), r: 4, fill: color, 'data-value': value});
        dot.append(svgNode('title', {}, `${label} · ${series.name}: ${format(value, chart.unit)}`));
        svg.append(dot);
      });
      svg.prepend(svgNode('path', {d: path.trim(), fill: 'none', stroke: color, 'stroke-width': 2.4, 'stroke-linejoin': 'round', 'data-series': series.name}));
    });
    return svg;
  }
  function barChart(chart) {
    const rows = chart.series.flatMap((s, index) => s.points.map(p => ({...p, name: s.name, color: palette[index % palette.length]})));
    const values = rows.map(r => r.value).filter(finite);
    const low = Math.min(0, ...values), max = Math.max(0, ...values);
    const high = max === low ? low + 1 : max;
    const width = 600, step = 52, height = Math.max(130, rows.length * step + 54);
    const left = 190, right = 108, top = 12, plotWidth = width - left - right;
    const x = v => left + (v - low) / (high - low) * plotWidth;
    const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': chart.title});
    svg.append(svgNode('title', {}, chart.title));
    svg.append(svgNode('desc', {}, 'Столбцы начинаются от нуля. Точные значения приведены в таблице под графиком.'));
    svg.append(svgNode('line', {x1: x(0), x2: x(0), y1: top, y2: height - 32, stroke: '#7d939f'}));
    rows.forEach((row, i) => {
      const y = top + i * step;
      const label = row.label.length > 24 ? row.label.slice(0, 23) + '…' : row.label;
      const caption = svgNode('text', {x: left - 12, y: y + 23, fill: '#c8d5df', 'text-anchor': 'end', 'font-size': 12}, label);
      caption.append(svgNode('title', {}, row.label));
      svg.append(caption);
      if (finite(row.value)) {
        const bar = svgNode('rect', {x: Math.min(x(0), x(row.value)), y: y + 7, width: Math.abs(x(row.value) - x(0)), height: 23, rx: 3, fill: row.color, 'data-value': row.value});
        bar.append(svgNode('title', {}, `${row.label} · ${row.name}: ${format(row.value, chart.unit)}`));
        svg.append(bar);
      }
      svg.append(svgNode('text', {x: width - right + 10, y: y + 23, fill: '#e1edf4', 'font-size': 12}, format(row.value, chart.unit, true)));
    });
    return svg;
  }
  function valuesTable(chart) {
    const details = node('details', 'chart-values');
    details.append(node('summary', '', 'Показать точные значения графика'));
    const wrap = node('div', 'table-wrap');
    const table = node('table');
    table.append(node('caption', 'sr-only', chart.title + ' — данные MCP'));
    const head = node('thead'), heading = node('tr');
    [chart.x_label || 'Категория / дата', 'Ряд', chart.y_label || `Значение, ${units[chart.unit]}`].forEach(label => {
      const th = node('th', '', label); th.scope = 'col'; heading.append(th);
    });
    head.append(heading);table.append(head);
    const body = node('tbody');
    chart.series.forEach(series => series.points.forEach(point => {
      const row = node('tr');
      row.append(node('td', '', point.label), node('td', '', series.name));
      const value = node('td', '', format(point.value, chart.unit));
      if (finite(point.value)) value.dataset.value = String(point.value);
      row.append(value); body.append(row);
    }));
    table.append(body);wrap.append(table);details.append(wrap);
    return details;
  }
  function render(raw, trace, onSource) {
    const chart = normalized(raw);
    if (!chart) return null;
    const refs = Array.isArray(chart.source_refs) ? chart.source_refs : [chart.source_ref];
    const verified = refs.filter(ref => typeof ref === 'string' && trace.some(t => t.ref === ref && t.ok));
    if (!verified.length) return null;
    const card = node('section', 'evidence-chart');
    card.dataset.chartKind = chart.kind;
    card.append(node('h3', '', chart.title || 'Результат анализа'));
    const filterLabels = {product: 'Продукт', region: 'Регион', risk_band: 'Рейтинг'};
    const selectedFilters = Object.entries(chart.filters || {}).filter(([key, value]) => Object.hasOwn(filterLabels, key) && value != null).map(([key, value]) => `${filterLabels[key]}: ${value}`);
    const scope = [chart.dataset_id ? 'Набор ' + chart.dataset_id : '', chart.start_date && chart.end_date ? `${chart.start_date} — ${chart.end_date}` : chart.as_of || '', ...selectedFilters].filter(Boolean).join(' · ');
    if (scope) card.append(node('p', 'chart-scope', scope));
    const available = chart.series.some(s => s.points.some(p => finite(p.value)));
    if (available) {
      const plot = node('div', 'answer-plot');
      plot.append(chart.kind === 'line' ? lineChart(chart) : barChart(chart));
      card.append(plot);
    } else card.append(node('p', 'chart-empty', 'Нет числовых данных для графика. Пропуски не заменяются нулями.'));
    if (chart.series.length > 1) {
      const legend = node('div', 'chart-series');
      chart.series.forEach((s, index) => {
        const item = node('span', '', s.name), dot = node('i');
        dot.style.background = palette[index % palette.length];item.prepend(dot);legend.append(item);
      });
      card.append(legend);
    }
    const source = node('div', 'chart-source', 'Данные MCP: ');
    verified.forEach(ref => {
      const button = node('button', 'source-ref', `[${ref}]`);button.type = 'button';
      button.addEventListener('click', () => onSource(ref));source.append(button);
    });
    card.append(source);
    if (chart.note) card.append(node('p', 'chart-note', chart.note));
    card.append(valuesTable(chart));
    return card;
  }
  return {render};
})();
