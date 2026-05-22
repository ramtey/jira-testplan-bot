/**
 * Icon set — Lucide / Tabler style. Stroke 1.75 default, 2 for emphasis.
 * Pass size={n} to override the 16px default.
 */

function Icon({ name, size = 16, stroke = 1.75, className, style }) {
  const props = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: stroke,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    className,
    style,
    "aria-hidden": "true",
  }
  switch (name) {
    case 'chevron-right':  return <svg {...props}><path d="m9 18 6-6-6-6"/></svg>
    case 'chevron-down':   return <svg {...props}><path d="m6 9 6 6 6-6"/></svg>
    case 'chevron-up':     return <svg {...props}><path d="m18 15-6-6-6 6"/></svg>
    case 'chevron-left':   return <svg {...props}><path d="m15 18-6-6 6-6"/></svg>
    case 'check':          return <svg {...props}><path d="m5 12 5 5L20 7"/></svg>
    case 'x':              return <svg {...props}><path d="M18 6 6 18M6 6l12 12"/></svg>
    case 'plus':           return <svg {...props}><path d="M12 5v14M5 12h14"/></svg>
    case 'minus':          return <svg {...props}><path d="M5 12h14"/></svg>
    case 'arrow-right':    return <svg {...props}><path d="M5 12h14m-7-7 7 7-7 7"/></svg>
    case 'arrow-left':     return <svg {...props}><path d="M19 12H5m7 7-7-7 7-7"/></svg>
    case 'arrow-up-right': return <svg {...props}><path d="M7 17 17 7M7 7h10v10"/></svg>
    case 'external':       return <svg {...props}><path d="M15 3h6v6"/><path d="m10 14 11-11"/><path d="M21 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h6"/></svg>
    case 'search':         return <svg {...props}><circle cx="11" cy="11" r="7"/><path d="m21 21-3.5-3.5"/></svg>
    case 'refresh':        return <svg {...props}><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>
    case 'filter':         return <svg {...props}><path d="M22 3H2l8 9.5V19l4 2v-8.5L22 3Z"/></svg>
    case 'star':           return <svg {...props}><path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2Z"/></svg>
    case 'pin':            return <svg {...props}><path d="m12 17 .01 5M5 10v5h14v-5"/><path d="M5 10 8 4h8l3 6"/></svg>
    case 'menu':           return <svg {...props}><path d="M3 6h18M3 12h18M3 18h18"/></svg>
    case 'panel-left':     return <svg {...props}><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/></svg>
    case 'sparkles':       return <svg {...props}><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></svg>
    case 'wand':           return <svg {...props}><path d="m15 4-1.5 1.5M4 20l9-9M15 9l-1.5-1.5M19 5l-1.5-1.5M21 9l-1.5 1.5"/><path d="m9 9 6 6"/></svg>
    case 'scan':           return <svg {...props}><path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/></svg>
    case 'flag':           return <svg {...props}><path d="M4 22V4M4 4h14l-3 5 3 5H4"/></svg>
    case 'bug':            return <svg {...props}><path d="M8 2l2 2M16 2l-2 2M12 20v-9M20 8h-2M6 8H4M20 14h-3M7 14H4M19 19l-2.5-2M5 19l2.5-2"/><rect x="8" y="6" width="8" height="14" rx="4"/></svg>
    case 'beaker':         return <svg {...props}><path d="M5 3h14M6 3v6L3 18a2 2 0 0 0 2 3h14a2 2 0 0 0 2-3l-3-9V3"/></svg>
    case 'circuit':        return <svg {...props}><path d="M11 3v3a2 2 0 0 1-2 2H6M21 11h-3a2 2 0 0 0-2 2v3M3 13h3a2 2 0 0 1 2 2v3M11 21v-3a2 2 0 0 1 2-2h3"/></svg>
    case 'shield':         return <svg {...props}><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/></svg>
    case 'paperclip':      return <svg {...props}><path d="m21 9-9.5 9.5a4 4 0 0 1-5.7-5.7l9.5-9.5a3 3 0 0 1 4.2 4.2L9 17"/></svg>
    case 'git-pull':       return <svg {...props}><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="18" r="3"/><path d="M6 9v6M15 18H9M18 15V8a2 2 0 0 0-2-2h-2"/></svg>
    case 'git-branch':     return <svg {...props}><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>
    case 'git-commit':     return <svg {...props}><circle cx="12" cy="12" r="4"/><path d="M1.05 12H8M16 12h7.05"/></svg>
    case 'code':           return <svg {...props}><path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/></svg>
    case 'file-text':      return <svg {...props}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/></svg>
    case 'download':       return <svg {...props}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>
    case 'upload':         return <svg {...props}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12"/></svg>
    case 'send':           return <svg {...props}><path d="m22 2-7 20-4-9-9-4 20-7Z"/></svg>
    case 'message':        return <svg {...props}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
    case 'user':           return <svg {...props}><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>
    case 'users':          return <svg {...props}><circle cx="9" cy="7" r="4"/><path d="M3 21a6 6 0 0 1 12 0"/><circle cx="17" cy="7" r="3"/><path d="M21 21a4 4 0 0 0-3-4"/></svg>
    case 'clock':          return <svg {...props}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
    case 'history':        return <svg {...props}><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l4 2"/></svg>
    case 'image':          return <svg {...props}><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></svg>
    case 'link':           return <svg {...props}><path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/></svg>
    case 'copy':           return <svg {...props}><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
    case 'info':           return <svg {...props}><circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/></svg>
    case 'alert':          return <svg {...props}><path d="M10.3 3.86 1.82 18a2 2 0 0 0 1.7 3h16.96a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><path d="M12 9v4M12 17h.01"/></svg>
    case 'alert-circle':   return <svg {...props}><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>
    case 'check-circle':   return <svg {...props}><circle cx="12" cy="12" r="9"/><path d="m9 12 2 2 4-4"/></svg>
    case 'x-circle':       return <svg {...props}><circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/></svg>
    case 'square':         return <svg {...props}><rect x="3" y="3" width="18" height="18" rx="2"/></svg>
    case 'play':           return <svg {...props}><path d="m6 4 14 8-14 8V4Z"/></svg>
    case 'stop':           return <svg {...props}><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>
    case 'key':            return <svg {...props}><circle cx="7.5" cy="15.5" r="4.5"/><path d="m11 12 9-9M18 5l3 3M15 8l3 3"/></svg>
    case 'lock':           return <svg {...props}><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
    case 'eye':            return <svg {...props}><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg>
    case 'sun':            return <svg {...props}><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>
    case 'moon':           return <svg {...props}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/></svg>
    case 'figma':          return <svg {...props}><path d="M5 5.5A2.5 2.5 0 0 1 7.5 3H12v5H7.5A2.5 2.5 0 0 1 5 5.5ZM12 3h4.5a2.5 2.5 0 0 1 0 5H12V3Zm0 5h4.5a2.5 2.5 0 0 1 0 5H12V8Zm0 5h-4.5a2.5 2.5 0 0 0 0 5H12v-5Zm0 0a2.5 2.5 0 1 1 5 0 2.5 2.5 0 0 1-5 0Z"/></svg>
    case 'github':         return <svg {...props}><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
    case 'list':           return <svg {...props}><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>
    case 'grid':           return <svg {...props}><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
    case 'columns':        return <svg {...props}><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 3v18"/></svg>
    case 'database':       return <svg {...props}><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/></svg>
    case 'layers':         return <svg {...props}><path d="m12 2 10 6-10 6L2 8l10-6Z"/><path d="m2 16 10 6 10-6M2 12l10 6 10-6"/></svg>
    case 'help':           return <svg {...props}><circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/></svg>
    case 'dots':           return <svg {...props}><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>
    case 'arrow-down-right': return <svg {...props}><path d="M7 7l10 10M17 7v10H7"/></svg>
    case 'split':          return <svg {...props}><path d="M16 3h5v5M14 15l7-7M8 3H3v5M3 16v5h5M21 16v5h-5M10 14l-7 7"/></svg>
    case 'trash':          return <svg {...props}><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
    default: return <svg {...props}><circle cx="12" cy="12" r="9"/></svg>
  }
}

export default Icon
