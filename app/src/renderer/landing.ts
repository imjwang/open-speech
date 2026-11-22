// Carries ?engine=…&token=… from the landing page onto the page links.
const params = window.location.search
for (const id of ['bar', 'reader']) {
  const a = document.getElementById(id) as HTMLAnchorElement | null
  if (a && params) a.href = `${a.getAttribute('href') ?? ''}${params}`
}
export {}
