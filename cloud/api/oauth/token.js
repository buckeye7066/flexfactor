import { endpoint, jsonBody } from "../../lib/http.js";
import { oauthPoll } from "../../lib/service.js";

export default endpoint({ methods: ["POST"] }, async (request) => {
  const body = await jsonBody(request);
  return oauthPoll(body.device_code);
});
