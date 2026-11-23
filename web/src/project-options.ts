/** Roots identify projects; labels distinguish equal folder names. */
export function projectOptions(records: readonly { root: string; project: string }[]) {
  const projects = [...new Map(records.map(record => [record.root, record.project])).entries()];
  return projects.map(([root, name]) => ({
    value: root,
    label: projects.some(([otherRoot, otherName]) => otherRoot !== root && otherName === name)
      ? `${name} (${root})` : name,
  })).sort((a, b) => a.label.localeCompare(b.label));
}
