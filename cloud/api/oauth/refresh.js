import { endpoint, jsonBody } from "../../lib/http.js";
import { oauthRefresh } from "../../lib/service.js";

export default endpoint({ methods: ["POST"] }, async (request) => {
  const body = await jsonBody(request);
  return oauthRefresh(body.refresh_token);
});
