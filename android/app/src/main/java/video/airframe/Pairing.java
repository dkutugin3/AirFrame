package video.airframe;

import java.net.URI;
import java.util.HashMap;
import java.util.Map;

/** Strict local QR format. Credentials are never persisted. */
final class Pairing {
    final String address, key;
    private Pairing(String address,String key){this.address=address;this.key=key;}
    static Pairing parse(String text) {
        try {
            if(text==null || text.length()>512)throw new IllegalArgumentException();
            URI uri=new URI(text);
            if(!"airframe".equals(uri.getScheme()) || !"connect".equals(uri.getRawAuthority())
                    || !"".equals(uri.getPath()) || uri.getFragment()!=null)throw new IllegalArgumentException();
            Map<String,String> values=new HashMap<>();
            for(String part:uri.getRawQuery().split("&")){
                String[] pair=part.split("=",-1);
                if(pair.length!=2 || values.put(pair[0],pair[1])!=null)throw new IllegalArgumentException();
            }
            String host=values.get("host"),port=values.get("port"),key=values.get("key");
            if(values.size()!=4 || !"1".equals(values.get("v")) || host==null
                    || !host.matches("(?:[0-9]{1,3}\\.){3}[0-9]{1,3}")
                    || port==null || !port.matches("[0-9]{1,5}")
                    || key==null || !key.matches("[A-Za-z0-9_-]{32,128}"))throw new IllegalArgumentException();
            for(String octet:host.split("\\."))if(Integer.parseInt(octet)>255)throw new IllegalArgumentException();
            int number=Integer.parseInt(port);
            if(number<1 || number>65535)throw new IllegalArgumentException();
            return new Pairing(host+":"+number,key);
        }catch(Exception error){throw new IllegalArgumentException("Неверный QR AirFrame. Сканируйте код в приложении на компьютере.");}
    }
}
