/** Byte counts as a human-readable string. Binary units, so the numbers agree with
 *  what a file manager reports rather than with the disk vendor's arithmetic. */
function formatBytes(bytes) {
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let n = Number(bytes) || 0;
    let unit = 0;
    while (n >= 1024 && unit < units.length - 1) { n /= 1024; unit++; }
    return `${unit && n < 100 ? n.toFixed(1) : Math.round(n)} ${units[unit]}`;
}
