// XLSX authoring uses @oai/artifact-tool. Runtime application needs only the
// checked-in workbook; regenerate records/oracles with teaching_dataset.py.
import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const [input, output, previewPath] = process.argv.slice(2);
if (!input || !output) {
  throw new Error('Usage: node build_teaching_workbook.mjs RECORDS.json OUTPUT.xlsx [PREVIEW.png]');
}
const data = JSON.parse(await fs.readFile(input, 'utf8'));
if (data.columns.length !== 9 || data.rows.length !== 14400) throw new Error('Unexpected teaching dataset shape');
const workbook = Workbook.create();
const sheet = workbook.worksheets.add('Портфель');
const rows = data.rows.map(row => row.map((value, column) => column === 1 ? new Date(`${value}T00:00:00.000Z`) : value));
sheet.getRange(`A1:I${rows.length + 1}`).values = [data.columns, ...rows];
sheet.showGridLines = false;
sheet.freezePanes.freezeRows(1);
sheet.freezePanes.freezeColumns(1);
sheet.getRange(`A1:I${rows.length + 1}`).format.font = {name:'Arial', size:10, color:'#182536'};
sheet.getRange(`A1:I${rows.length + 1}`).format.rowHeight = 18;
sheet.getRange(`A1:I${rows.length + 1}`).format.verticalAlignment = 'center';
sheet.getRange('A1:I1').format = {fill:'#15324D', font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'}, rowHeight:28};
sheet.getRange('A1:I1').format.horizontalAlignment = 'center';
sheet.getRange('A1:A14401').format.columnWidth = 21;
sheet.getRange('B1:B14401').format.columnWidth = 15;
sheet.getRange('C1:C14401').format.columnWidth = 24;
sheet.getRange('D1:D14401').format.columnWidth = 20;
sheet.getRange('E1:E14401').format.columnWidth = 12;
sheet.getRange('F1:F14401').format.columnWidth = 19;
sheet.getRange('G1:I14401').format.columnWidth = 12;
sheet.getRange('B2:B14401').setNumberFormat('yyyy-mm-dd');
sheet.getRange('F2:F14401').setNumberFormat('#,##0.00');
sheet.getRange('G2:G14401').setNumberFormat('0');
sheet.getRange('H2:I14401').setNumberFormat('0.00%');
sheet.tables.add('A1:I14401', true, 'PortfolioSnapshots');
workbook.recalculate();
const inspected = await workbook.inspect({kind:'table',range:'Портфель!A1:I5',include:'values,formulas',tableMaxRows:5,tableMaxCols:9,maxChars:2000});
console.log(inspected.ndjson);
const errorScan = await workbook.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!',options:{useRegex:true,maxResults:20},maxChars:1000});
console.log(errorScan.ndjson);
await fs.mkdir(path.dirname(path.resolve(output)),{recursive:true});
const file = await SpreadsheetFile.exportXlsx(workbook);
await file.save(output);
if (previewPath) {
  const preview = await workbook.render({sheetName:'Портфель',range:'A1:I14',scale:1.5,format:'png'});
  await fs.mkdir(path.dirname(path.resolve(previewPath)),{recursive:true});
  await fs.writeFile(previewPath,new Uint8Array(await preview.arrayBuffer()));
}
console.log(JSON.stringify({file:output,rows:rows.length,sheets:1,formulaCells:0}));
