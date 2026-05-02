// static std::map<std::string, std::string> g_data;

// static void do_request(std::vector<std::string> &cmd, Buffer *out){

//   size_t header_pos = buf_size(out);

//   //placeholder for resp_len 
//   uint32_t placeholder = 0; //4 bytes
//   buf_append(out, (const uint8_t *)&placeholder, 4);

//   uint32_t status = RES_OK;

//   if(cmd.size() == 2 && cmd[0] == "get"){
//     auto it = g_data.find(cmd[1]);
//     if (it == g_data.end()){
//       status = RES_NX;
//       return;
//     }
//     const std::string &val = it->second;
//     buf_append(out, (const uint8_t *)val.data(), val.size());
//   } else if (cmd.size() == 3 && cmd[0] == "set"){
//     g_data[cmd[1]].swap(cmd[2]);
//   } else if (cmd.size() == 2 && cmd[0] == "del"){
//     g_data.erase(cmd[1]);
//   } else {
//     status = RES_ERR;
//   }

//   buf_append(out, (const uint8_t *)&status, 4);

//   uint32_t resp_len = (uint32_t)(buf_size(out) - header_pos - 4);
//   memcpy(buf_data(out) + header_pos, &resp_len, 4);
// }