/**
 * Which web addresses the person asked for.
 *
 * A web address is the one way what is on screen could be carried off the
 * machine, and a web page can talk the model into asking for one. So an
 * address counts as the person's own request in only two cases:
 *
 *   named        it is the real address of a well-known site they named:
 *                "YouTube" means youtube.com, and that site's own subdomains
 *                such as m.youtube.com; never youtube.lol, and saying
 *                "Andrew" never vouches for andrew.lol
 *   spelled out  they said the address itself: "youtube dot lol", or a
 *                transcript that reads "youtube.lol"
 *
 * Anything else is asked about first. Nothing becomes impossible; some sites
 * just ask.
 */
import { normalize, saidIn, wordsOf } from "./words.js";

/** Well-known sites, by the names people call them, and the domains that are really theirs. */
export const KNOWN_SITES: ReadonlyArray<{ names: string[]; domains: string[] }> = [
  { names: ["YouTube"], domains: ["youtube.com", "youtu.be"] },
  { names: ["Google"], domains: ["google.com"] },
  { names: ["Gmail"], domains: ["gmail.com", "google.com"] },
  { names: ["Wikipedia"], domains: ["wikipedia.org"] },
  { names: ["Amazon"], domains: ["amazon.com"] },
  { names: ["Netflix"], domains: ["netflix.com"] },
  { names: ["Facebook"], domains: ["facebook.com"] },
  { names: ["Instagram"], domains: ["instagram.com"] },
  { names: ["Twitter"], domains: ["twitter.com", "x.com"] },
  { names: ["LinkedIn"], domains: ["linkedin.com"] },
  { names: ["Reddit"], domains: ["reddit.com"] },
  { names: ["GitHub"], domains: ["github.com"] },
  { names: ["GitLab"], domains: ["gitlab.com"] },
  { names: ["Stack Overflow"], domains: ["stackoverflow.com"] },
  { names: ["Spotify"], domains: ["spotify.com"] },
  { names: ["Apple"], domains: ["apple.com"] },
  { names: ["iCloud"], domains: ["icloud.com"] },
  { names: ["Microsoft"], domains: ["microsoft.com"] },
  { names: ["Outlook"], domains: ["outlook.com", "live.com", "office.com"] },
  { names: ["Bing"], domains: ["bing.com"] },
  { names: ["Yahoo"], domains: ["yahoo.com"] },
  { names: ["DuckDuckGo"], domains: ["duckduckgo.com"] },
  { names: ["ChatGPT"], domains: ["chatgpt.com", "openai.com"] },
  { names: ["OpenAI"], domains: ["openai.com"] },
  { names: ["Claude"], domains: ["claude.ai"] },
  { names: ["Anthropic"], domains: ["anthropic.com"] },
  { names: ["Pinterest"], domains: ["pinterest.com"] },
  { names: ["TikTok"], domains: ["tiktok.com"] },
  { names: ["Twitch"], domains: ["twitch.tv"] },
  { names: ["Discord"], domains: ["discord.com"] },
  { names: ["Slack"], domains: ["slack.com"] },
  { names: ["Zoom"], domains: ["zoom.us"] },
  { names: ["Dropbox"], domains: ["dropbox.com"] },
  { names: ["Notion"], domains: ["notion.so", "notion.com"] },
  { names: ["Figma"], domains: ["figma.com"] },
  { names: ["Canva"], domains: ["canva.com"] },
  { names: ["eBay"], domains: ["ebay.com"] },
  { names: ["Etsy"], domains: ["etsy.com"] },
  { names: ["Walmart"], domains: ["walmart.com"] },
  { names: ["PayPal"], domains: ["paypal.com"] },
  { names: ["Airbnb"], domains: ["airbnb.com"] },
  { names: ["Uber"], domains: ["uber.com"] },
  { names: ["ESPN"], domains: ["espn.com"] },
  { names: ["CNN"], domains: ["cnn.com"] },
  { names: ["BBC"], domains: ["bbc.com", "bbc.co.uk"] },
  { names: ["New York Times", "NYTimes"], domains: ["nytimes.com"] },
  { names: ["IMDb"], domains: ["imdb.com"] },
  { names: ["Quora"], domains: ["quora.com"] },
  { names: ["WhatsApp"], domains: ["whatsapp.com"] },
  { names: ["Craigslist"], domains: ["craigslist.org"] },
  { names: ["Zillow"], domains: ["zillow.com"] },
  { names: ["Yelp"], domains: ["yelp.com"] },
  { names: ["Tripadvisor"], domains: ["tripadvisor.com"] },
  { names: ["Expedia"], domains: ["expedia.com"] },
  { names: ["Booking.com"], domains: ["booking.com"] },
  { names: ["Hulu"], domains: ["hulu.com"] },
  { names: ["Vimeo"], domains: ["vimeo.com"] },
  { names: ["SoundCloud"], domains: ["soundcloud.com"] },
  { names: ["Coursera"], domains: ["coursera.org"] },
  { names: ["Duolingo"], domains: ["duolingo.com"] },
];

/**
 * Endings under which each name belongs to someone different: country
 * endings such as co.uk, and hosting services whose subdomains are other
 * people's sites (anyone's-name.github.io). The site is the name before them.
 */
const SHARED_ENDINGS = new Set([
  // two-part country endings
  "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "com.au", "net.au", "org.au", "edu.au",
  "gov.au", "co.nz", "org.nz", "co.jp", "ne.jp", "or.jp", "ac.jp", "co.kr", "or.kr", "com.br", "net.br", "org.br",
  "com.cn", "net.cn", "org.cn", "com.hk", "com.tw", "com.sg", "com.my", "co.in", "net.in", "org.in", "co.za",
  "com.mx", "com.ar", "com.tr", "co.il", "com.ua", "co.id", "com.ph", "com.vn", "com.pk", "com.ng", "com.eg",
  "com.sa",
  // hosting where every subdomain is someone else's site
  "github.io", "gitlab.io", "bitbucket.io", "github.dev", "vercel.app", "netlify.app", "pages.dev", "workers.dev",
  "herokuapp.com", "web.app", "firebaseapp.com", "appspot.com", "blogspot.com", "wordpress.com", "tumblr.com",
  "substack.com", "medium.com", "notion.site", "azurewebsites.net", "cloudfront.net", "amazonaws.com",
  "glitch.me", "repl.co", "replit.app", "replit.dev", "ngrok.io", "ngrok.app", "ngrok-free.app", "onrender.com",
  "fly.dev", "railway.app", "up.railway.app", "surge.sh", "webflow.io", "wixsite.com", "myshopify.com",
  "squarespace.com", "framer.app", "framer.website", "carrd.co", "deno.dev", "readthedocs.io", "itch.io",
  "neocities.org", "weebly.com",
]);

/** The part of a host that names one owner's site: youtube.com for m.youtube.com, bbc.co.uk for news.bbc.co.uk. */
export function siteOf(host: string): string[] {
  const labels = host.toLowerCase().replace(/\.$/, "").replace(/^www\./, "").split(".").filter(Boolean);
  // An IP address is only ever itself.
  if (labels.length > 0 && labels.every((label) => /^\d+$/.test(label))) return labels;
  for (let n = Math.min(labels.length - 1, 3); n >= 1; n -= 1) {
    if (SHARED_ENDINGS.has(labels.slice(-n).join("."))) return labels.slice(-(n + 1));
  }
  return labels.slice(-2);
}

/** Did the person ask for this web address, by naming its site or spelling it out? */
export function siteSaidIn(host: string, words: string): boolean {
  const site = siteOf(host);
  if (site.length === 0) return false;
  if (spelledOut(site, wordsOf(spokenDots(words)))) return true;
  const domain = site.join(".");
  return KNOWN_SITES.some((known) => known.domains.includes(domain) && known.names.some((name) => saidIn(name, words)));
}

/** "youtube.lol" in a transcript is said "youtube dot lol". A full stop between sentences is not. */
function spokenDots(text: string): string {
  return text.replace(/([a-z0-9])\.(?=[a-z0-9])/gi, "$1 dot ");
}

/**
 * The site's labels in order, each one word or several in a row, with "dot"
 * between them: "youtube dot lol", "my site dot com" for my-site.com. Without
 * the "dot", "youtube lol" is just two words.
 */
function spelledOut(labels: string[], said: string[]): boolean {
  const want = labels.map(normalize);
  if (want.some((label) => !label)) return false;
  const from = (i: number, k: number): boolean => {
    let run = "";
    for (let j = i; j < said.length && run.length < want[k].length; j += 1) {
      run += said[j];
      if (run === want[k]) return k === want.length - 1 || (said[j + 1] === "dot" && from(j + 2, k + 1));
    }
    return false;
  };
  for (let i = 0; i < said.length; i += 1) if (from(i, 0)) return true;
  return false;
}
