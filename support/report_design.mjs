import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { FileBlob, SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const work = path.dirname(fileURLToPath(import.meta.url));
if (process.argv[2] === 'inspect') {
  const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(path.join(work, 'partner-monitor/demo-report.xlsx')));
  console.log((await wb.inspect({kind:'sheet',include:'id,name',maxChars:1500})).ndjson);
  const preview = await wb.render({sheetName:'Отчёт',range:'A1:G6',scale:1,format:'png'});
  await fs.writeFile(path.join(work,'report-before.png'),new Uint8Array(await preview.arrayBuffer()));
  console.log('Existing report rendered');
}
if (process.argv[2] === 'build') {
  const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(path.join(work,'sample-generated.xlsx')));
  const overview = wb.worksheets.getItem('Отчёт');
  const dates = JSON.parse(await fs.readFile(path.join(work,'sample-dates.json'),'utf8'));
  for (const d of dates) {
    const range = wb.worksheets.getItem(d.sheet).getRange(d.cell);
    range.values = [[(Date.parse(d.value+'Z')-Date.UTC(1899,11,30))/86400000]];
    range.setNumberFormat('yyyy-mm-dd hh:mm');
    range.format.horizontalAlignment = 'center';
  }
  overview.getRange('A8:L8').format.horizontalAlignment = 'center';
  overview.getRange('E9:J12').setNumberFormat('#,##0');
  overview.getRange('E9').values = [[11]];
  wb.recalculate();
  if (overview.getRange('G9').values[0][0] !== 9) throw new Error('Balance did not recalculate when plan changed');
  overview.getRange('E9').values = [[10]];
  wb.recalculate();
  console.log((await wb.inspect({kind:'table',range:'Отчёт!A8:L12',include:'values,formulas',tableMaxRows:5,tableMaxCols:12,maxChars:3500})).ndjson);
  console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#SPILL!',options:{useRegex:true,maxResults:30},summary:'final formula error scan',maxChars:1000})).ndjson);
  const summary = overview.getRange('E9:G12').values;
  const plans = summary.reduce((n,r)=>n+Number(r[0]),0);
  const posted = summary.reduce((n,r)=>n+Number(r[1]),0);
  const remaining = summary.reduce((n,r)=>n+Number(r[2]),0);
  if (plans!==40 || posted!==13 || remaining!==27) throw new Error(`Summary mismatch ${plans}/${posted}/${remaining}`);
  const previews = [ ['Отчёт','A1:L13','overview'],['Отчёт','A17:L40','post-card'],
    ['Недоступные посты','A1:J3','unavailable'],['Реестр публикаций','A1:F6','registry'],
    ['История просмотров','A1:F5','history'],['Партнёры','A1:H5','briefs'] ];
  for (const [sheetName,range,name] of previews) {
    if (process.argv[3] === 'card-only' && name !== 'post-card') continue;
    const preview = await wb.render({sheetName,range,scale:1,format:'png'});
    await fs.writeFile(path.join(work,`report-${name}.png`),new Uint8Array(await preview.arrayBuffer()));
  }
  const out = path.join(work,'../outputs/unified-report-example.xlsx');
  await (await SpreadsheetFile.exportXlsx(wb)).save(out);
  console.log(`Verified totals: plan ${plans}, published ${posted}, remaining ${remaining}. Saved ${out}`);
}
if (process.argv[2] === 'diagnose') {
  const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(path.join(work,'sample-generated.xlsx')));
  const range = wb.worksheets.getItem('Отчёт').getRange('K9');
  console.log(wb.help('range.setNumberFormat',{include:'index,examples,notes',maxChars:2000}).ndjson);
  console.log('before',JSON.stringify(range.values),JSON.stringify(range.format.numberFormat));
  range.values = [[46303.5833333333]];
  range.setNumberFormat('yyyy-mm-dd hh:mm');
  console.log('after',JSON.stringify(range.values),JSON.stringify(range.format.numberFormat));
  console.log((await wb.inspect({kind:'computedStyle',sheetId:'Отчёт',range:'K9',maxChars:1500})).ndjson);
  const preview = await wb.render({sheetName:'Отчёт',range:'J8:L10',scale:1,format:'png'});
  await fs.writeFile(path.join(work,'date-diagnostic.png'),new Uint8Array(await preview.arrayBuffer()));
}
process.exit(0);
