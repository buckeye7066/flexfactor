import { endpoint } from "../lib/http.js";
import { providerPublicKey } from "../lib/service.js";

export default endpoint({ methods: ["GET"], authenticated: true }, async (request, token) => {
  return providerPublicKey(token, String(request.query?.repository || ""));
});
