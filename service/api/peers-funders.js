// GET /api/peers-funders?eins=<EIN,EIN,...>&min_peers=&years=&limit=&format=text
import { endpoint } from '../lib/http.js'
import { peersFunders } from '../lib/core.js'
import { peersText } from '../lib/format.js'
export default endpoint(peersFunders, peersText)
